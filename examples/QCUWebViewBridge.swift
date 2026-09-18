//
//  QCUWebViewBridge.swift
//  QCU in-app debug bridge — reference implementation for WKWebView apps.
//
//  Why this exists: WKWebView content is NOT externally automatable. The AX
//  tree shows only the AXWebArea shell, and Apple's remote-inspection path
//  (Safari Develop menu) requires private entitlements plus manual GUI
//  operation — unusable as an automation backend. For apps YOU control,
//  embedding this bridge exposes a localhost-only, token-authenticated
//  evaluate endpoint that QCU's `desktop_jsbridge` layer can drive with full
//  DOM semantics.
//
//  Usage (one line):
//      QCUWebViewBridge.shared.attach(webView)
//
//  Security model:
//   - Listens on the loopback interface only (NWParameters requiredInterfaceType
//     = .loopback); unreachable from the network.
//   - Every evaluate request must carry the per-launch random token as the
//     X-QCU-Token header; the token lives only in the bridge file, readable
//     solely by this user's processes.
//   - The bridge file is removed atexit so stale ports are never advertised.
//
//  Bridge file: $QCU_BRIDGE_DIR/<pid>.json, defaulting to ~/.qcu/bridges/.
//  Sandboxed apps: their home is the container — either run unsandboxed for
//  debugging, or set QCU_BRIDGE_DIR to a path inside an app group both sides
//  can reach, and set the same variable when invoking `qcu`.
//

import Foundation
import Network
import WebKit

final class QCUWebViewBridge: @unchecked Sendable {
    static let shared = QCUWebViewBridge()

    private let token = UUID().uuidString
    private let queue = DispatchQueue(label: "qcu.webview.bridge")
    private var listener: NWListener?
    private weak var webView: WKWebView?
    private var bridgeFileURL: URL?

    private init() {}

    /// Attach the WKWebView to expose and start serving. Safe to call once.
    func attach(_ webView: WKWebView) {
        self.webView = webView
        start()
    }

    // ------------------------------------------------------------------

    private func start() {
        guard listener == nil else { return }
        let params = NWParameters.tcp
        params.requiredInterfaceType = .loopback
        guard let listener = try? NWListener(using: params, on: .any) else { return }
        self.listener = listener
        listener.newConnectionHandler = { [weak self] conn in self?.handle(conn) }
        listener.stateUpdateHandler = { [weak self] state in
            if case .ready = state, let self, let port = self.listener?.port {
                self.publish(port: Int(port.rawValue))
            }
        }
        listener.start(queue: queue)
        atexit { QCUWebViewBridge.cleanupBridgeFile() }
    }

    private static var fileToRemove: URL?
    private static func cleanupBridgeFile() {
        if let url = fileToRemove { try? FileManager.default.removeItem(at: url) }
    }

    private func publish(port: Int) {
        let dir = ProcessInfo.processInfo.environment["QCU_BRIDGE_DIR"]
            .map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".qcu/bridges", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("\(ProcessInfo.processInfo.processIdentifier).json")
        let info: [String: Any] = [
            "pid": ProcessInfo.processInfo.processIdentifier,
            "port": port,
            "token": token,
            "app": Bundle.main.bundleIdentifier ?? ProcessInfo.processInfo.processName,
            "kind": "wkwebview-jsbridge",
            "started_at": Date().timeIntervalSince1970,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: info) {
            try? data.write(to: file, options: .atomic)
            // The token inside is the only auth for JS evaluation in this app;
            // keep the bridge file readable by this user alone.
            try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                                                   ofItemAtPath: file.path)
            bridgeFileURL = file
            QCUWebViewBridge.fileToRemove = file
        }
    }

    // ------------------------------------------------------------------
    // Minimal HTTP/1.1 handling — the bridge speaks exactly two routes.
    // ------------------------------------------------------------------

    private func handle(_ conn: NWConnection) {
        conn.start(queue: queue)
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, _, _ in
            guard let self, let data, let request = String(data: data, encoding: .utf8) else {
                conn.cancel(); return
            }
            self.route(request, conn)
        }
    }

    private func respond(_ conn: NWConnection, status: Int, body: String) {
        let reason = status == 200 ? "OK" : (status == 403 ? "Forbidden" : "Bad Request")
        let payload = body.data(using: .utf8) ?? Data()
        let head = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(payload.count)\r\nConnection: close\r\n\r\n"
        var out = head.data(using: .utf8) ?? Data()
        out.append(payload)
        conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func route(_ request: String, _ conn: NWConnection) {
        let head = request.components(separatedBy: "\r\n\r\n").first ?? ""
        let lines = head.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { respond(conn, status: 400, body: "{}"); return }
        let parts = requestLine.components(separatedBy: " ")
        let method = parts.count > 0 ? parts[0] : ""
        let path = parts.count > 1 ? parts[1] : ""
        let authed = lines.contains { $0.lowercased() == "x-qcu-token: \(token.lowercased())" }

        if method == "GET", path == "/qcu/info" {
            // Benign metadata only; no page content without the token.
            respond(conn, status: 200, body: """
            {"ok":true,"app":"\(Bundle.main.bundleIdentifier ?? "")","pid":\(ProcessInfo.processInfo.processIdentifier),"webview_attached":\(webView != nil)}
            """)
            return
        }
        if method == "POST", path == "/qcu/evaluate" {
            guard authed else { respond(conn, status: 403, body: #"{"ok":false,"error":"bad token"}"#); return }
            let body = String(request.components(separatedBy: "\r\n\r\n").dropFirst().joined(separator: "\r\n\r\n"))
            guard let data = body.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let expression = obj["expression"] as? String, !expression.isEmpty else {
                respond(conn, status: 400, body: #"{"ok":false,"error":"expression required"}"#); return
            }
            evaluate(expression, conn)
            return
        }
        respond(conn, status: 400, body: #"{"ok":false,"error":"unknown route"}"#)
    }

    private func evaluate(_ expression: String, _ conn: NWConnection) {
        DispatchQueue.main.async { [weak self] in
            guard let webView = self?.webView else {
                self?.respond(conn, status: 400, body: #"{"ok":false,"error":"no webview attached"}"#)
                return
            }
            webView.evaluateJavaScript(expression) { [weak self] value, error in
                guard let self else { conn.cancel(); return }
                if let error {
                    self.respond(conn, status: 200,
                                 body: "{\"ok\":false,\"error\":\"" + error.localizedDescription.jsonEscaped + "\"}")
                    return
                }
                // Serialize the JS value as JSON so the Python side can parse
                // uniformly. undefined/null become NSNull -> JSON null.
                let result: Any = value ?? NSNull()
                if JSONSerialization.isValidJSONObject(["value": result]),
                   let data = try? JSONSerialization.data(withJSONObject: ["ok": true, "value": result]),
                   let text = String(data: data, encoding: .utf8) {
                    self.respond(conn, status: 200, body: text)
                } else {
                    self.respond(conn, status: 200, body: #"{"ok":true,"value":null}"#)
                }
            }
        }
    }
}

private extension String {
    var jsonEscaped: String {
        var s = replacingOccurrences(of: "\\", with: "\\\\")
        s = s.replacingOccurrences(of: "\"", with: "\\\"")
        s = s.replacingOccurrences(of: "\n", with: "\\n")
        return s
    }
}

import Foundation

/// Manages WebSocket connection to /ws/patch-progress for real-time Ansible output
@MainActor
class WebSocketService: ObservableObject {

    @Published var messages: [String] = []
    @Published var isConnected = false
    @Published var patchComplete = false

    private var webSocketTask: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)

    // MARK: - Connect

    func connect() {
        guard let baseURL = KeychainHelper.serverURL else { return }

        // Convert http(s) to ws(s)
        var wsURL = baseURL
            .replacingOccurrences(of: "https://", with: "wss://")
            .replacingOccurrences(of: "http://", with: "ws://")
        wsURL += "/ws/patch-progress"

        // Add token as query parameter if available
        if let token = KeychainHelper.sessionToken {
            wsURL += "?token=\(token)"
        }

        guard let url = URL(string: wsURL) else { return }

        webSocketTask = session.webSocketTask(with: url)
        webSocketTask?.resume()
        isConnected = true
        patchComplete = false
        messages = []

        receiveMessage()
    }

    // MARK: - Disconnect

    func disconnect() {
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        webSocketTask = nil
        isConnected = false
    }

    // MARK: - Receive Loop

    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            Task { @MainActor in
                guard let self = self else { return }

                switch result {
                case .success(let message):
                    switch message {
                    case .string(let text):
                        self.handleMessage(text)
                    case .data(let data):
                        if let text = String(data: data, encoding: .utf8) {
                            self.handleMessage(text)
                        }
                    @unknown default:
                        break
                    }
                    // Continue listening
                    self.receiveMessage()

                case .failure:
                    self.isConnected = false
                }
            }
        }
    }

    // MARK: - Message Handling

    private func handleMessage(_ text: String) {
        messages.append(text)

        // Check for completion signals
        let lower = text.lowercased()
        if lower.contains("patch complete") || lower.contains("all hosts patched") ||
           lower.contains("patching finished") || lower.contains("\"status\":\"complete\"") {
            patchComplete = true
        }
    }

    // MARK: - Clear

    func clear() {
        messages = []
        patchComplete = false
    }
}

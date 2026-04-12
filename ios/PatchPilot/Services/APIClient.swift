import Foundation

/// Central HTTP client for all PatchPilot API calls
@MainActor
class APIClient: ObservableObject {

    static let shared = APIClient()

    @Published var baseURL: String = ""

    private let session: URLSession
    private let decoder: JSONDecoder

    init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 120
        self.session = URLSession(configuration: config)
        self.decoder = JSONDecoder()

        // Restore saved server URL
        if let saved = KeychainHelper.serverURL {
            self.baseURL = saved
        }
    }

    // MARK: - Configuration

    var isConfigured: Bool {
        !baseURL.isEmpty
    }

    func configure(serverURL: String) {
        // Normalize: remove trailing slash
        var url = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while url.hasSuffix("/") { url.removeLast() }
        self.baseURL = url
        KeychainHelper.serverURL = url
    }

    // MARK: - Request Building

    private func buildRequest(path: String, method: String = "GET", body: (any Encodable)? = nil) throws -> URLRequest {
        guard let url = URL(string: "\(baseURL)\(path)") else {
            throw APIError.invalidURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        // Inject Bearer token if available
        if let token = KeychainHelper.sessionToken {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        if let body = body {
            request.httpBody = try JSONEncoder().encode(body)
        }

        return request
    }

    // MARK: - Generic Request Methods

    func get<T: Decodable>(_ path: String) async throws -> T {
        let request = try buildRequest(path: path)
        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return try decoder.decode(T.self, from: data)
    }

    func post<T: Decodable>(_ path: String, body: (any Encodable)? = nil) async throws -> T {
        let request = try buildRequest(path: path, method: "POST", body: body)
        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return try decoder.decode(T.self, from: data)
    }

    func put<T: Decodable>(_ path: String, body: (any Encodable)? = nil) async throws -> T {
        let request = try buildRequest(path: path, method: "PUT", body: body)
        let (data, response) = try await session.data(for: request)
        try validateResponse(response)
        return try decoder.decode(T.self, from: data)
    }

    func delete(_ path: String) async throws {
        let request = try buildRequest(path: path, method: "DELETE")
        let (_, response) = try await session.data(for: request)
        try validateResponse(response)
    }

    /// POST that returns no typed body (just validates status)
    func postVoid(_ path: String, body: (any Encodable)? = nil) async throws {
        let request = try buildRequest(path: path, method: "POST", body: body)
        let (_, response) = try await session.data(for: request)
        try validateResponse(response)
    }

    // MARK: - Connectivity Check

    func checkConnectivity() async throws -> Bool {
        let request = try buildRequest(path: "/api/setup/status")
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { return false }
        return http.statusCode == 200
    }

    // MARK: - Response Validation

    private func validateResponse(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }

        switch http.statusCode {
        case 200...299:
            return
        case 401:
            // Session expired — clear token
            KeychainHelper.sessionToken = nil
            throw APIError.unauthorized
        case 403:
            throw APIError.forbidden
        case 404:
            throw APIError.notFound
        case 500...599:
            throw APIError.serverError(http.statusCode)
        default:
            throw APIError.httpError(http.statusCode)
        }
    }
}

// MARK: - Error Types

enum APIError: LocalizedError {
    case invalidURL
    case invalidResponse
    case unauthorized
    case forbidden
    case notFound
    case serverError(Int)
    case httpError(Int)
    case decodingError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid server URL"
        case .invalidResponse: return "Invalid response from server"
        case .unauthorized: return "Session expired. Please log in again."
        case .forbidden: return "You don't have permission for this action"
        case .notFound: return "Resource not found"
        case .serverError(let code): return "Server error (\(code))"
        case .httpError(let code): return "Request failed (\(code))"
        case .decodingError(let error): return "Data error: \(error.localizedDescription)"
        }
    }
}

import Foundation
import Security

/// Secure storage for session tokens and server URL using iOS Keychain
enum KeychainHelper {

    private static let service = "com.patchpilot.ios"

    // MARK: - Keys
    static let tokenKey = "session_token"
    static let serverURLKey = "server_url"

    // MARK: - Save
    @discardableResult
    static func save(key: String, value: String) -> Bool {
        guard let data = value.data(using: .utf8) else { return false }
        delete(key: key)

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock
        ]

        let status = SecItemAdd(query as CFDictionary, nil)
        return status == errSecSuccess
    }

    // MARK: - Read
    static func read(key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)

        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    // MARK: - Delete
    @discardableResult
    static func delete(key: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key
        ]

        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    // MARK: - Convenience
    static var sessionToken: String? {
        get { read(key: tokenKey) }
        set {
            if let value = newValue {
                save(key: tokenKey, value: value)
            } else {
                delete(key: tokenKey)
            }
        }
    }

    static var serverURL: String? {
        get { read(key: serverURLKey) }
        set {
            if let value = newValue {
                save(key: serverURLKey, value: value)
            } else {
                delete(key: serverURLKey)
            }
        }
    }
}

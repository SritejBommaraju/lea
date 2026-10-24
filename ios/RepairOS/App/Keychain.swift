import Foundation
import Security

/// The gateway device token (from POST /v1/devices/pair). The phone holds no other secret.
enum Keychain {
    private static let service = "com.repairos.gateway"
    private static let account = "device-token"

    static func token() -> String? {
        var query = base
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess, let data = out as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func setToken(_ token: String) throws {
        deleteToken()
        var item = base
        item[kSecValueData as String] = Data(token.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw GatewayError.message("Keychain write failed (\(status)).") }
    }

    static func deleteToken() {
        SecItemDelete(base as CFDictionary)
    }

    private static var base: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }
}

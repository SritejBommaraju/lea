import Foundation
import Security

/// Bridge URL and paired-device token, kept in the Keychain (this device only).
enum BridgeSettings {
    private static let service = "app.lea.bridge"

    static var baseURL: URL? {
        get { read("url").flatMap(URL.init(string:)) }
        set { write("url", newValue?.absoluteString) }
    }

    static var token: String? {
        get { read("token") }
        set { write("token", newValue) }
    }

    static func client() -> BridgeClient? {
        guard let url = baseURL, let token, !token.isEmpty else { return nil }
        return BridgeClient(baseURL: url, token: token)
    }

    private static func read(_ account: String) -> String? {
        var item: CFTypeRef?
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: account, kSecReturnData as String: true]
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func write(_ account: String, _ value: String?) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
        guard let value else { return }
        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }
}

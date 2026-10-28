import Foundation
import Security

/// What the phone keeps in the Keychain: the gateway device token (from POST /v1/devices/pair) and the product code
/// the person scanned or typed (`ProductCode`: which server and which product). Neither is a secret the phone made up
/// or shows; both stay out of backups and logs.
enum Keychain {
    private static let service = "com.repairos.gateway"
    private static let tokenAccount = "device-token"
    private static let productAccount = "product-code"

    static func token() -> String? {
        read(tokenAccount).flatMap { String(data: $0, encoding: .utf8) }
    }

    static func setToken(_ token: String) throws {
        try write(Data(token.utf8), account: tokenAccount)
    }

    static func deleteToken() {
        SecItemDelete(base(tokenAccount) as CFDictionary)
    }

    /// The saved product code, or nil when none was added or the saved bytes do not read back.
    static func product() -> ProductCode? {
        read(productAccount).flatMap { try? JSONDecoder().decode(ProductCode.self, from: $0) }
    }

    static func setProduct(_ code: ProductCode) throws {
        try write(JSONEncoder().encode(code), account: productAccount)
    }

    static func deleteProduct() {
        SecItemDelete(base(productAccount) as CFDictionary)
    }

    private static func read(_ account: String) -> Data? {
        var query = base(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess else { return nil }
        return out as? Data
    }

    private static func write(_ data: Data, account: String) throws {
        SecItemDelete(base(account) as CFDictionary)
        var item = base(account)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw GatewayError.message("The phone could not save this (Keychain error \(status)).") }
    }

    private static func base(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }
}

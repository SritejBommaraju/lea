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
        #if DEBUG
        unsigned.set(nil, tokenAccount)
        #endif
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
        #if DEBUG
        unsigned.set(nil, productAccount)
        #endif
    }

    private static func read(_ account: String) -> Data? {
        var query = base(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &out)
        #if DEBUG
        if status == errSecMissingEntitlement { return unsigned.value[account] }
        #endif
        guard status == errSecSuccess else { return nil }
        return out as? Data
    }

    #if DEBUG
    /// An unsigned simulator build (CI) has no Keychain access group, so every Keychain call fails with
    /// errSecMissingEntitlement. Debug builds then keep the value in memory so the app and the screenshot tests still work.
    /// Release builds never do this.
    private final class Unsigned: @unchecked Sendable {
        private let lock = NSLock()
        private var store: [String: Data] = [:]
        var value: [String: Data] { lock.lock(); defer { lock.unlock() }; return store }
        func set(_ data: Data?, _ account: String) { lock.lock(); store[account] = data; lock.unlock() }
    }
    private static let unsigned = Unsigned()
    #endif

    private static func write(_ data: Data, account: String) throws {
        SecItemDelete(base(account) as CFDictionary)
        #if DEBUG
        unsigned.set(nil, account)
        #endif
        var item = base(account)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(item as CFDictionary, nil)
        #if DEBUG
        if status == errSecMissingEntitlement { unsigned.set(data, account); return }
        #endif
        guard status == errSecSuccess else { throw GatewayError.message("The phone could not save this (Keychain error \(status)).") }
    }

    private static func base(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }
}

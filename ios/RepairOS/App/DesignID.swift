import Foundation

/// The design id of the product being scanned: the design engine's project for it, printed on the product or typed
/// in Settings. It goes to the server with the session; with none, the server uses its own default design, and with
/// neither the report says the design is unknown.
enum DesignID {
    static let key = "design_id"

    static var current: String? {
        UserDefaults.standard.string(forKey: key)?.trimmedOrNil
    }
}

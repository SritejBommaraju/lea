import Foundation

/// The design id of the product being scanned: the design engine's project for it. It comes from the product code the
/// person scanned or typed (`ProductCode`), else from the id typed in Settings; the code wins, since it is newer. It goes to the server with the session; with none, the server uses its own default design, and with
/// neither the report says the design is unknown.
enum DesignID {
    static let key = "design_id"

    static var current: String? {
        if let code = ProductCode.stored() { return code.product }
        return UserDefaults.standard.string(forKey: key)?.trimmedOrNil
    }
}

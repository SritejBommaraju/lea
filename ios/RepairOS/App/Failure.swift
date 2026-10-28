import Foundation

/// What went wrong, said plainly: a short title, one or two sentences on what happened, and the one thing to do next.
/// Every place that can fail in front of a customer maps its error through here, so nobody sees "The server answered
/// 502: {...}" or a Swift error. The raw error still goes to `diag.jsonl` where the code logs it; this is only the words.
struct Failure: Equatable, Sendable {
    /// The one next step. The screen decides how to run it; `openSettings` is always the phone's Settings for lea.
    enum Action: Equatable, Sendable {
        case retry, openSettings, reconnect, shareScan, enterCode, freeSpace

        var label: String {
            switch self {
            case .retry: "Try again"
            case .openSettings: "Open Settings"
            case .reconnect: "Connect again"
            case .shareScan: "Share the scan files"
            case .enterCode: "Add product code"
            case .freeSpace: "Delete old scans"
            }
        }
    }

    /// Where the error came from, when the same code means different things (a 401 while pairing is a wrong code).
    enum Context: Sendable { case general, pairing, upload, report }

    var title: String
    var message: String
    var action: Action?
    var symbol = "exclamationmark.circle"

    /// The title and message as one sentence pair, for places that have one text line (a status row, an error string).
    var text: String { "\(title). \(message)" }

    // MARK: Errors

    static func from(_ error: Error, context: Context = .general) -> Failure {
        if let wrapped = error as? Wrapped { return wrapped.failure }
        if let url = error as? URLError { return network(url) }
        if let gateway = error as? GatewayError {
            switch gateway {
            case .http(let code, let body): return http(code, body, context)
            case .message(let text): return Failure(title: "Can't continue", message: text, action: nil)
            }
        }
        let ns = error as NSError
        if ns.domain == NSCocoaErrorDomain, ns.code == NSFileWriteOutOfSpaceError { return storageFull }
        if ns.domain == NSPOSIXErrorDomain, ns.code == Int(ENOSPC) { return storageFull }
        return Failure(title: "Something went wrong",
                       message: "Try again. If it keeps happening, share the scan files so we can look.",
                       action: .retry)
    }

    /// A Failure that travels as a Swift error (the upload queue throws errors, not structs).
    struct Wrapped: Error { let failure: Failure }

    private static func network(_ e: URLError) -> Failure {
        switch e.code {
        case .notConnectedToInternet, .networkConnectionLost, .dataNotAllowed, .internationalRoamingOff, .callIsActive:
            return Failure(title: "No internet",
                           message: "The phone is offline. Turn on Wi-Fi or mobile data. The scan stays on the phone and sends when you are back online.",
                           action: .retry, symbol: "wifi.slash")
        case .cannotFindHost, .dnsLookupFailed:
            return Failure(title: "Can't find the lea server",
                           message: "The server address doesn't exist. Check it in Settings, or add the product code again.",
                           action: .reconnect, symbol: "network.slash")
        case .cannotConnectToHost, .timedOut:
            return Failure(title: "Can't reach the lea server",
                           message: "The server didn't answer. Check that it is on, and that the phone is on the right Wi-Fi.",
                           action: .retry, symbol: "network.slash")
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot,
             .serverCertificateNotYetValid, .clientCertificateRejected:
            return Failure(title: "The connection isn't secure",
                           message: "The server's certificate wasn't accepted, so lea won't send your photos. Check the server address in Settings.",
                           action: .reconnect, symbol: "lock.slash")
        case .appTransportSecurityRequiresSecureConnection:
            return Failure(title: "The address must start with https",
                           message: "lea only sends photos over a secure connection to servers on the internet. Check the address in Settings.",
                           action: .reconnect, symbol: "lock.slash")
        case .cancelled:
            return Failure(title: "Stopped", message: "That was cancelled.", action: .retry)
        default:
            return Failure(title: "Can't reach the lea server", message: "Something is in the way of the connection. Try again in a minute.",
                           action: .retry, symbol: "network.slash")
        }
    }

    private static func http(_ code: Int, _ body: String, _ context: Context) -> Failure {
        let lowered = body.lowercased()
        switch code {
        case 401, 403:
            if context == .pairing {
                return Failure(title: "That pairing code isn't right",
                               message: "Check it and try again. It is on the paperwork that came with the product, or from the person who set up the server.",
                               action: nil, symbol: "key")
            }
            return Failure(title: "This phone isn't connected",
                           message: "The server doesn't know this phone any more. Connect it again.",
                           action: .reconnect, symbol: "key")
        case 404:
            if context == .pairing {
                return Failure(title: "That isn't a lea server",
                               message: "Nothing that works with lea answered at this address. Check the address.", action: nil)
            }
            return Failure(title: "The server doesn't have that",
                           message: "The server can't find what the app asked for. The app may need an update.", action: .retry)
        case 409 where lowered.contains("design is unknown"):
            return Failure(title: "The server doesn't know this product",
                           message: "Add the product code that came with it, so lea knows what to check against.",
                           action: .enterCode, symbol: "shippingbox")
        case 409:
            return Failure(title: "The server is busy with this scan", message: "Wait a moment, then try again.", action: .retry)
        case 413:
            return Failure(title: "A file was too big to send",
                           message: "Scan again from a bit further back, or share the scan files so we can look.", action: .shareScan)
        case 422:
            return Failure(title: "The server couldn't read this scan",
                           message: "Scan the product again. If it happens again, share the scan files so we can look.", action: .shareScan)
        case 429:
            return Failure(title: "The server is busy", message: "Too many requests at once. Wait a minute, then try again.", action: .retry)
        case 500...599:
            return Failure(title: "The server had a problem", message: "It isn't your phone. Try again in a few minutes.", action: .retry)
        default:
            return Failure(title: "The server said no",
                           message: "It answered with code \(code). Try again, or share the scan files so we can look.", action: .retry)
        }
    }

    // MARK: Fixed situations

    static var storageFull: Failure {
        Failure(title: "The phone is out of space",
                message: "lea can't save any more of this scan. Delete old scans in lea's Settings, or free space in the iPhone's own Settings, then scan again.",
                action: .freeSpace, symbol: "internaldrive")
    }

    static func storageLow(freeBytes: Int64, needed: Int64) -> Failure {
        Failure(title: "Not enough space to scan",
                message: "A scan needs about \(needed >> 30) GB free and the phone has \(freeBytes >> 20) MB. Delete old scans in lea's Settings, or free space in the iPhone's own Settings.",
                action: .freeSpace, symbol: "internaldrive")
    }

    static let cameraDenied = Failure(title: "Camera is off for lea",
                                      message: "lea scans with the camera. Turn Camera on for lea in Settings, then come back.",
                                      action: .openSettings, symbol: "camera")

    static let bluetoothDenied = Failure(title: "Bluetooth is off for lea",
                                         message: "lea needs Bluetooth to read your meter. Turn it on for lea in Settings, or type the reading instead.",
                                         action: .openSettings, symbol: "antenna.radiowaves.left.and.right.slash")

    static let notPaired = Failure(title: "Not connected yet",
                                   message: "The scan is saved on the phone. Connect to the lea server and it sends from there.",
                                   action: .reconnect, symbol: "link.badge.plus")

    static let reportTimeout = Failure(title: "The report is taking longer than usual",
                                       message: "Your scan is safe on the server. Check back in a few minutes, or try again now.",
                                       action: .retry, symbol: "hourglass")
}

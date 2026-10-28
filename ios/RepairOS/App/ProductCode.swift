import Foundation

/// The code that comes with a product: which lea server judges its scans, and which product it is. It is printed as a
/// QR code (or written on the paperwork) so nobody types a server address or describes an unknown object.
///
/// Two spellings, both read by `ProductCode.parse`:
///
///     repairos://g/<server>/<product>              the QR code
///     repairos://g/<server>/<product>?pair=<code>  the same, with the pairing code in it
///     <product>@<server>                           the short form to type
///
/// `<server>` is a host name or address with an optional port (`lea.example.com`, `192.168.1.10:8000`). A host on the
/// local network (an IP address in a private range, `localhost`, a `.local` name or a name with no dot) is reached over
/// http, every other host over https. `<product>` is the design id the server judges against: letters, digits, `.`,
/// `_` and `-`, up to 200 characters. A pairing code in the QR code is a shared secret, so print one only on a code that
/// stays with the person who owns the product.
struct ProductCode: Codable, Equatable, Sendable {
    /// `https://lea.example.com` or `http://192.168.1.10:8000`: the address the app sends to.
    var gateway: String
    var product: String
    /// The pairing code, when the code carried one. Used once to connect; never stored.
    var pairing: String?

    enum CodingKeys: String, CodingKey { case gateway, product }

    init(gateway: String, product: String, pairing: String? = nil) {
        self.gateway = gateway
        self.product = product
        self.pairing = pairing
    }

    var host: String { gateway.components(separatedBy: "://").last ?? gateway }

    static let example = "lamp-v2@lea.example.com"

    /// What to say when a code can't be read.
    static let help = "A product code looks like \(example) or repairos://g/lea.example.com/lamp-v2."

    /// The saved code, if the person added one.
    static func stored() -> ProductCode? { Keychain.product() }

    /// Reads a code, or nil when it isn't one. Nothing is guessed: a missing product or a bad host is nil.
    static func parse(_ raw: String) -> ProductCode? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 400 else { return nil }
        let lower = text.lowercased()
        if lower.hasPrefix("repairos://g/") {
            var rest = String(text.dropFirst("repairos://g/".count))
            var pairing: String?
            if let q = rest.firstIndex(of: "?") {
                let query = rest[rest.index(after: q)...]
                rest = String(rest[..<q])
                for pair in query.split(separator: "&") {
                    let kv = pair.split(separator: "=", maxSplits: 1).map(String.init)
                    if kv.count == 2, kv[0] == "pair" { pairing = kv[1].removingPercentEncoding?.trimmingCharacters(in: .whitespaces) }
                }
            }
            let parts = rest.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
            guard parts.count == 2, let code = make(host: parts[0], product: parts[1]) else { return nil }
            return ProductCode(gateway: code.gateway, product: code.product, pairing: pairing?.isEmpty == true ? nil : pairing)
        }
        if let at = text.firstIndex(of: "@") {
            return make(host: String(text[text.index(after: at)...]), product: String(text[..<at]))
        }
        return nil
    }

    private static func make(host: String, product: String) -> ProductCode? {
        let host = host.trimmingCharacters(in: .whitespaces)
        let product = product.trimmingCharacters(in: .whitespaces)
        guard matches(host, #"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$"#),
              matches(product, #"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"#) else { return nil }
        let scheme = isLocal(host) ? "http" : "https"
        return ProductCode(gateway: "\(scheme)://\(host.lowercased())", product: product)
    }

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }

    /// True for a server on the local network, which has no certificate to check.
    static func isLocal(_ hostAndPort: String) -> Bool {
        let host = hostAndPort.split(separator: ":", maxSplits: 1).first.map(String.init)?.lowercased() ?? ""
        if host == "localhost" || host.hasSuffix(".local") || !host.contains(".") { return true }
        let nums = host.split(separator: ".").compactMap { Int($0) }
        guard nums.count == 4, host.split(separator: ".").count == 4 else { return false }
        switch (nums[0], nums[1]) {
        case (10, _), (127, _), (192, 168), (169, 254): return true
        case (172, 16...31): return true
        default: return false
        }
    }
}

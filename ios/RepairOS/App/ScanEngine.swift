import Foundation

enum EngineKind: String, Codable, Sendable {
    /// Our ARKit scanner (tap-to-lock, coverage dome, auto 12 MP stills; LiDAR points when present).
    case arkit

    /// A record saved by the removed Object Capture engine (`objectCapture`) opens as a normal scan.
    init(from decoder: Decoder) throws {
        _ = try decoder.singleValueContainer().decode(String.self)
        self = .arkit
    }

    var title: String { "Scan" }
}

extension SIMD3 where Scalar == Float {
    var doubles: [Double] { [Double(x), Double(y), Double(z)] }
}

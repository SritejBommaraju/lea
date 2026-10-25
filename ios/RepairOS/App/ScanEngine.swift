import Foundation

enum EngineKind: String, Codable, Sendable {
    /// Default: our ARKit scanner (tap-to-lock, coverage dome, auto 12 MP stills; LiDAR points when present).
    case arkit
    /// Apple Object Capture, behind the `--object-capture` developer flag only.
    case objectCapture

    var title: String {
        switch self {
        case .arkit: "Scan"
        case .objectCapture: "Object Capture scan"
        }
    }
}

extension SIMD3 where Scalar == Float {
    var doubles: [Double] { [Double(x), Double(y), Double(z)] }
}

import ARKit
import AVFoundation

/// One full-resolution still from the running AR session, in the same world frame as the scan (plan §4).
@MainActor
enum HighResCapture {
    /// - Parameter maxDimensions: ask for the sensor's largest photo size (iOS 26 `defaultPhotoSettings`). The
    ///   probe decides whether that works; ARKit may reject some photo settings.
    static func capture(session: ARSession, format: ARConfiguration.VideoFormat?, maxDimensions: Bool,
                        requested: inout String?) async throws -> ARFrame {
        if maxDimensions, #available(iOS 26.0, *), let format {
            let settings = format.defaultPhotoSettings
            let dims = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera?
                .activeFormat.supportedMaxPhotoDimensions ?? []
            if let largest = dims.max(by: { Int($0.width) * Int($0.height) < Int($1.width) * Int($1.height) }) {
                settings.maxPhotoDimensions = largest
                requested = "\(largest.width)x\(largest.height)"
            }
            return try await session.captureHighResolutionFrame(using: settings)
        }
        return try await session.captureHighResolutionFrame()
    }
}

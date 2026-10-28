import ARKit
import SwiftUI
import AVFoundation
import Foundation
import UIKit

/// What this phone can do, read from the APIs at runtime, never from a device list.
/// Adapted from ObjectScanner's Core/DeviceCapabilities.swift (Apache-2.0), see THIRD_PARTY_NOTICES.md.
struct DeviceCapabilities: Sendable {
    let model: String
    let ios: String
    let worldTracking: Bool
    /// Rear LiDAR depth.
    let sceneDepth: Bool
    let meshReconstruction: Bool
    let rearCameras: [String]
    let videoFormats: [String]

    /// Our ARKit scanner.
    var engine: EngineKind { .arkit }

    var contract: Device {
        Device(appVersion: "repairos/\(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0")",
               ios: ios, model: model)
    }

    @MainActor
    static func current() -> DeviceCapabilities {
        var info = utsname()
        uname(&info)
        let model = withUnsafeBytes(of: &info.machine) { raw in
            String(decoding: raw.prefix(while: { $0 != 0 }), as: UTF8.self)
        }
        let rear = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInWideAngleCamera, .builtInUltraWideCamera, .builtInTelephotoCamera, .builtInLiDARDepthCamera],
            mediaType: .video, position: .back
        ).devices.map(\.localizedName)
        return DeviceCapabilities(
            model: model,
            ios: UIDevice.current.systemVersion,
            worldTracking: ARWorldTrackingConfiguration.isSupported,
            sceneDepth: ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
            meshReconstruction: ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
            rearCameras: rear,
            videoFormats: ARWorldTrackingConfiguration.supportedVideoFormats.map(\.label)
        )
    }
}

extension ARConfiguration.VideoFormat {
    var label: String { "\(key)\(isRecommendedForHighResolutionFrameCapturing ? " hi-res" : "")" }

    /// Unique per format: the same size and rate can exist for several camera modules.
    var key: String {
        let device = captureDeviceType.rawValue.replacingOccurrences(of: "AVCaptureDeviceTypeBuiltIn", with: "")
        return "\(Int(imageResolution.width))x\(Int(imageResolution.height))@\(framesPerSecond) \(device)"
    }

    var pixels: Int { Int(imageResolution.width) * Int(imageResolution.height) }

    /// Formats worth trying for close-ups, best guess first: the HD 4:3 stream that allows high-res stills (cheap
    /// keyframes, less heat), the framework's own pick, and the largest high-res-capable stream.
    static func stillCandidates() -> [ARConfiguration.VideoFormat] {
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats
        let hi = formats.filter(\.isRecommendedForHighResolutionFrameCapturing)
        var out: [ARConfiguration.VideoFormat] = []
        func add(_ f: ARConfiguration.VideoFormat?) {
            if let f, !out.contains(where: { $0.key == f.key }) { out.append(f) }
        }
        add(hi.filter { Int($0.imageResolution.width) == 1920 && Int($0.imageResolution.height) == 1440 }
            .min { abs($0.framesPerSecond - 30) < abs($1.framesPerSecond - 30) })
        add(ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing)
        add(hi.max { $0.pixels < $1.pixels })
        if out.isEmpty { add(formats.first) }
        return out
    }
}

enum StillResolution: String, Codable, Sendable {
    case mp48, mp24, mp12

    init(pixels: Int) {
        self = pixels >= 40_000_000 ? .mp48 : pixels >= 20_000_000 ? .mp24 : .mp12
    }
}

/// What `captureHighResolutionFrame` really returns on this phone and iOS build (plan §4.1). Nothing public says
/// whether 48 MP is reachable inside an AR session, so the app measures it once and keeps the answer.
struct StillProbeResult: Codable, Sendable {
    struct Attempt: Codable, Sendable {
        var format: String
        var requested: String?
        var width = 0
        var height = 0
        var latencyMs = 0
        var hasSceneDepth = false
        var error: String?
    }

    var iosBuild: String
    var model: String
    var supportedMaxPhotoDimensions: [String] = []
    var attempts: [Attempt] = []
    /// The stream format the capture session should run (`VideoFormat.key`).
    var chosenFormat: String?
    /// Whether to pass photo settings with the largest dimensions (iOS 26+).
    var useMaxDimensions = false
    var resolution: StillResolution = .mp12
    var probedAt = Date()

    private static let defaultsKey = "stillProbe"

    static func cached() -> StillProbeResult? {
        guard let data = UserDefaults.standard.data(forKey: defaultsKey),
              let result = try? JSONDecoder().decode(StillProbeResult.self, from: data),
              result.iosBuild == currentBuild else { return nil }
        return result
    }

    func store() {
        if let data = try? JSONEncoder().encode(self) { UserDefaults.standard.set(data, forKey: Self.defaultsKey) }
    }

    static var currentBuild: String { ProcessInfo.processInfo.operatingSystemVersionString }
}

/// Runs a short throwaway AR session and captures one high-resolution frame per candidate format, with and
/// without the largest photo dimensions.
@MainActor
enum StillProbe {
    static func run() async -> StillProbeResult {
        var result = StillProbeResult(iosBuild: StillProbeResult.currentBuild, model: DeviceCapabilities.current().model)
        guard ARWorldTrackingConfiguration.isSupported, await cameraAllowed() else {
            result.attempts.append(.init(format: "-", error: "camera not available or not allowed"))
            return result
        }
        let candidates = ARConfiguration.VideoFormat.stillCandidates()

        var best: (pixels: Int, format: String, maxDims: Bool)?
        var maxDims: [String: [String]] = [:]
        let session = ARSession()
        for format in candidates {
            let config = ARWorldTrackingConfiguration()
            config.videoFormat = format
            session.run(config, options: [.resetTracking])
            try? await Task.sleep(for: .seconds(2.5))
            if let device = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera {
                maxDims[format.key] = device.activeFormat.supportedMaxPhotoDimensions.map { "\($0.width)x\($0.height)" }
                result.supportedMaxPhotoDimensions = maxDims.map { "\($0.key): \($0.value.joined(separator: ", "))" }.sorted()
            }
            for useMax in [false, true] {
                if useMax, #unavailable(iOS 26.0) { continue }
                var attempt = StillProbeResult.Attempt(format: format.label)
                let start = Date()
                do {
                    let frame = try await HighResCapture.capture(session: session, format: format, maxDimensions: useMax,
                                                                 requested: &attempt.requested)
                    attempt.width = CVPixelBufferGetWidth(frame.capturedImage)
                    attempt.height = CVPixelBufferGetHeight(frame.capturedImage)
                    attempt.hasSceneDepth = frame.sceneDepth != nil
                    let pixels = attempt.width * attempt.height
                    if pixels > (best?.pixels ?? 0) { best = (pixels, format.key, useMax) }
                } catch {
                    attempt.error = error.localizedDescription
                }
                attempt.latencyMs = Int(Date().timeIntervalSince(start) * 1000)
                result.attempts.append(attempt)
            }
        }
        session.pause()
        if let best {
            result.chosenFormat = best.format
            result.useMaxDimensions = best.maxDims
            result.resolution = StillResolution(pixels: best.pixels)
        }
        log.info("still probe: \(String(describing: result.attempts), privacy: .public)")
        return result
    }

    static func cameraAllowed() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: true
        case .notDetermined: await AVCaptureDevice.requestAccess(for: .video)
        default: false
        }
    }
}

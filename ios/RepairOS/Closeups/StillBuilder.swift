import ARKit
import Foundation

/// Writes one still: full-resolution JPEG (q 0.9, sensor orientation so the intrinsics apply), depth and confidence
/// (the high-res frame's own when ARKit attaches it, else the nearest streamed LiDAR frame), and the `still` JSON
/// with the pose and intrinsics of the same frame.
enum StillBuilder {
    struct Input: @unchecked Sendable {
        let frame: ARFrame
        let stillID: String
        let scanID: String
        let sessionID: String
        let targetID: String?
        let targetReason: TargetReason?
        let viewCell: String?
        let lowLight: Bool
        let torchLevel: Float
        let autoFired: Bool
        /// Capture segment, and whether its poses share segment 0's frame (a relocalised continuation does; a flip
        /// or an unaligned continuation does not).
        let segment: Int
        var sameFrame = false
        /// JPEG quality: orbit stills 0.65 (the backend keeps ~10 of them), close-ups 0.8 (where the detail is).
        var jpegQuality = 0.65
        /// The nearest streamed depth, used when the high-res frame carries none, and its time gap.
        let nearestDepth: DepthCopy?
        let depthGapMs: Int?
        let iso: Double?
        let stillsDir: URL
    }

    static func write(_ input: Input) async throws -> Still {
        try await Task.detached(priority: .userInitiated) { try build(input) }.value
    }

    private static func build(_ i: Input) throws -> Still {
        let frame = i.frame
        let image = frame.capturedImage
        // Measured 12 MP sizes: q 0.72 ≈ 2.5–2.7 MB, q 0.82 ≈ 3.1 MB, q 0.9 ≈ 3.5 MB.
        let (w, h) = try ImageWriters.writeJPEG(image, to: i.stillsDir.appending(path: "\(i.stillID).jpg"), quality: i.jpegQuality)
        var depthFile: String?, confFile: String?
        if let depth = frame.sceneDepth {
            try ImageWriters.writeDepthPNG(depth.depthMap, to: i.stillsDir.appending(path: "\(i.stillID)_depth.png"))
            depthFile = "stills/\(i.stillID)_depth.png"
            if let conf = depth.confidenceMap {
                try ImageWriters.writeConfidencePNG(conf, to: i.stillsDir.appending(path: "\(i.stillID)_conf.png"))
                confFile = "stills/\(i.stillID)_conf.png"
            }
        } else if let d = i.nearestDepth {
            try ImageWriters.writeDepthPNG(d.depth, width: d.width, height: d.height,
                                           to: i.stillsDir.appending(path: "\(i.stillID)_depth.png"))
            depthFile = "stills/\(i.stillID)_depth.png"
            try d.confidence.withUnsafeBytes {
                try ImageWriters.writePNG(bytes: $0, width: d.width, height: d.height, bitsPerComponent: 8,
                                          bytesPerRow: d.width, bitmapInfo: CGBitmapInfo(),
                                          to: i.stillsDir.appending(path: "\(i.stillID)_conf.png"))
            }
            confFile = "stills/\(i.stillID)_conf.png"
        }
        let k = frame.camera.intrinsics
        // ARKit reports the high-resolution frame's intrinsics for its own image size; rescale defensively if a
        // device ever reports them for the stream size instead (the centre must sit near the image middle).
        var scale = 1.0
        let cx = Double(k[2][0])
        if cx > 0, abs(cx * 2 - Double(w)) / Double(w) > 0.2 { scale = Double(w) / (cx * 2) }
        let t = frame.camera.transform
        let q = simd_quatf(t)
        let normal: Bool = { if case .normal = frame.camera.trackingState { return true } else { return false } }()
        let target = i.targetID.map { TargetElement(boxMm: nil, harnessRows: nil, partRefs: nil, reason: i.targetReason, targetID: $0) }
        let still = Still(
            attachedToTargetID: nil, autoFired: i.autoFired,
            camera: StillCamera(frame: .arkitWorld,
                                positionM: [Double(t.columns.3.x), Double(t.columns.3.y), Double(t.columns.3.z)],
                                quaternionXyzw: [Double(q.imag.x), Double(q.imag.y), Double(q.imag.z), Double(q.real)]),
            cameraDesign: nil, capturedAt: Date(), confidenceFile: confFile, depthFile: depthFile,
            depthGapMS: depthFile == nil ? nil : i.depthGapMs, exposureS: frame.camera.exposureDuration > 0 ? frame.camera.exposureDuration : nil,
            heightPx: h,
            imageFile: "stills/\(i.stillID).jpg",
            intrinsics: StillIntrinsics(cx: cx * scale, cy: Double(k[2][1]) * scale,
                                        fx: Double(k[0][0]) * scale, fy: Double(k[1][1]) * scale),
            iso: i.iso, kind: target == nil ? .unguided : .guided, lowLight: i.lowLight ? true : nil,
            poseReliable: normal && (i.segment == 0 || i.sameFrame),
            scanID: i.scanID, segment: i.segment == 0 ? nil : i.segment, sessionID: i.sessionID, sharpness: SharpnessMeter.laplacianVariance(image),
            stillID: i.stillID, targets: target.map { [$0] } ?? [], torchLevel: Double(i.torchLevel), torchOn: i.torchLevel > 0,
            trackingState: normal ? .normal : .limited, userSkipped: nil, viewCell: i.viewCell, widthPx: w)
        try JSON.encoder.encode(still).write(to: i.stillsDir.appending(path: "\(i.stillID).json"), options: .atomic)
        return still
    }
}

import ARKit
import Foundation
import Vision

/// The person in the frame (the hand holding the object), as a mask at LiDAR depth resolution, so the tracker and the
/// cloud never take the hand for the object.
///
/// First choice: ARKit's own person segmentation (`.personSegmentationWithDepth`), which runs alongside `.sceneDepth`
/// in world tracking on LiDAR phones (checked at run time with `supportsFrameSemantics`). Fallback: Vision's hand pose
/// (21 joints per hand) drawn as finger capsules and a palm, ~7 Hz on the tracker's queue.
enum HandMask {
    /// Both semantics together on this phone.
    static var segmentationSupported: Bool {
        ARWorldTrackingConfiguration.supportsFrameSemantics([.sceneDepth, .personSegmentationWithDepth])
    }

    /// ARKit's person mask resampled to `w` × `h` (1 = person), nil when the frame has none.
    static func fromSegmentation(_ frame: ARFrame, width w: Int, height h: Int) -> [UInt8]? {
        guard let buf = frame.segmentationBuffer else { return nil }
        CVPixelBufferLockBaseAddress(buf, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buf, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buf) else { return nil }
        let sw = CVPixelBufferGetWidth(buf), sh = CVPixelBufferGetHeight(buf), row = CVPixelBufferGetBytesPerRow(buf)
        let p = base.assumingMemoryBound(to: UInt8.self)
        var out = [UInt8](repeating: 0, count: w * h)
        var any = false
        for y in 0..<h {
            let sy = min(sh - 1, y * sh / h)
            for x in 0..<w where p[sy * row + min(sw - 1, x * sw / w)] != 0 {
                out[y * w + x] = 1
                any = true
            }
        }
        return any ? out : nil
    }

    /// Joint chains of a hand (wrist → fingertip), for the capsules.
    static let chains: [[VNHumanHandPoseObservation.JointName]] = [
        [.wrist, .thumbCMC, .thumbMP, .thumbIP, .thumbTip],
        [.wrist, .indexMCP, .indexPIP, .indexDIP, .indexTip],
        [.wrist, .middleMCP, .middlePIP, .middleDIP, .middleTip],
        [.wrist, .ringMCP, .ringPIP, .ringDIP, .ringTip],
        [.wrist, .littleMCP, .littlePIP, .littleDIP, .littleTip],
    ]

    /// Vision hand pose on the grey image (sensor orientation; the phone is held upright, so Vision sees it turned
    /// `.right`), drawn into a depth-resolution mask: capsules ~1.1 cm wide along each finger (width from the depth at
    /// the joint) and the palm between the wrist and the knuckles. nil when no hand is found.
    static func fromHandPose(luma: GrayImage, depth: [Float], width w: Int, height h: Int, fx: Float) -> [UInt8]? {
        guard let cg = cgImage(luma) else { return nil }
        let request = VNDetectHumanHandPoseRequest()
        request.maximumHandCount = 2
        let handler = VNImageRequestHandler(cgImage: cg, orientation: .right)
        guard (try? handler.perform([request])) != nil, let hands = request.results, !hands.isEmpty else { return nil }
        var mask = [UInt8](repeating: 0, count: w * h)
        func depthAt(_ p: SIMD2<Float>) -> Float {
            let x = min(w - 1, max(0, Int(p.x))), y = min(h - 1, max(0, Int(p.y)))
            let d = depth[y * w + x]
            return d > 0.05 ? d : 0.35
        }
        func stamp(_ a: SIMD2<Float>, _ b: SIMD2<Float>, radius r: Float) {
            let lo = simd_min(a, b) - r, hi = simd_max(a, b) + r
            let ab = b - a, len2 = max(1e-6, simd_length_squared(ab))
            for y in max(0, Int(lo.y))...min(h - 1, max(0, Int(hi.y))) {
                for x in max(0, Int(lo.x))...min(w - 1, max(0, Int(hi.x))) {
                    let p = SIMD2(Float(x), Float(y))
                    let t = max(0, min(1, simd_dot(p - a, ab) / len2))
                    if simd_distance(p, a + t * ab) <= r { mask[y * w + x] = 1 }
                }
            }
        }
        var any = false
        for hand in hands {
            guard let pts = try? hand.recognizedPoints(.all) else { continue }
            // Vision's normalised point (lower-left origin, image seen `.right`) → depth pixel (sensor orientation).
            func px(_ name: VNHumanHandPoseObservation.JointName) -> SIMD2<Float>? {
                guard let p = pts[name], p.confidence > 0.3 else { return nil }
                let u = 1 - Float(p.location.y), v = 1 - Float(p.location.x)
                return SIMD2(u * Float(w), v * Float(h))
            }
            for chain in chains {
                let joints = chain.compactMap(px)
                guard joints.count >= 2 else { continue }
                for k in 1..<joints.count {
                    let r = max(2, fx * 0.0065 / depthAt(joints[k]))   // ~1.3 cm finger width
                    stamp(joints[k - 1], joints[k], radius: r)
                    any = true
                }
            }
            if let wrist = px(.wrist) {
                for k in [VNHumanHandPoseObservation.JointName.indexMCP, .middleMCP, .ringMCP, .littleMCP] {
                    if let m = px(k) { stamp(wrist, m, radius: max(3, fx * 0.012 / depthAt(m))) }
                }
            }
        }
        return any ? mask : nil
    }

    private static func cgImage(_ g: GrayImage) -> CGImage? {
        let data = Data(g.pixels) as CFData
        guard let provider = CGDataProvider(data: data) else { return nil }
        return CGImage(width: g.width, height: g.height, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: g.width,
                       space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: 0), provider: provider,
                       decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }
}

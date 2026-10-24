import Foundation

/// What the quality checks look at, measured at ~10 Hz on a downsampled frame off the main thread.
struct QualityMetrics: Sendable {
    var sharpness: Double            // Laplacian variance of the downsampled luma
    var medianSharpness: Double?     // the session's running median (nil until enough samples)
    var meanLuma: Double             // 0...1
    var ambient: Double?             // ARKit lightEstimate.ambientIntensity
    var distance: Float?             // camera to the object (LiDAR depth at the box centre, else geometry)
    var band: ClosedRange<Float>     // usable distance for this step
    var fill: Float                  // fraction of the frame the object's box covers
    var inFrame: Bool
    var trackingNormal: Bool
    var angularSpeed: Float          // rad/s
    var minFill: Float = 0.20
}

/// One accept/reject per frame with at most one reason, so the screen always shows a tick or a single fix.
/// Low light never blocks: it lowers the sharpness bar and marks the still.
struct QualityResult: Sendable, Equatable {
    var accept: Bool
    var reason: String?
    var lowLight: Bool
}

enum QualityGate {
    static let darkAmbient = 400.0
    static let darkLuma = 0.25
    static let maxTurn: Float = 40 * .pi / 180
    static let blurRatio = 0.7
    static let blurRatioLowLight = 0.6

    static func evaluate(_ m: QualityMetrics) -> QualityResult {
        let lowLight = (m.ambient.map { $0 < darkAmbient } ?? false) || m.meanLuma < darkLuma
        if !m.inFrame { return QualityResult(accept: false, reason: "Keep the object in frame", lowLight: lowLight) }
        if let d = m.distance, d > m.band.upperBound {
            return QualityResult(accept: false, reason: "Too far: move closer", lowLight: lowLight)
        }
        // Small objects cannot fill 20 % of the frame at the nearest usable distance; being at the near end counts.
        let atNearEnd = m.distance.map { $0 <= m.band.lowerBound * 1.15 } ?? false
        if m.fill < m.minFill, !atNearEnd {
            return QualityResult(accept: false, reason: "Too far: move closer", lowLight: lowLight)
        }
        if let d = m.distance, d < m.band.lowerBound * 0.8 {
            return QualityResult(accept: false, reason: "Too close: back up", lowLight: lowLight)
        }
        if m.angularSpeed > maxTurn {
            return QualityResult(accept: false, reason: "Blurry: hold still", lowLight: lowLight)
        }
        if let median = m.medianSharpness, m.sharpness < median * (lowLight ? blurRatioLowLight : blurRatio) {
            return QualityResult(accept: false, reason: "Blurry: hold still", lowLight: lowLight)
        }
        // Weak tracking never blocks: the still is kept with pose_reliable false.
        if !m.trackingNormal {
            return QualityResult(accept: true, reason: lowLight ? "Add light or move closer" : "Move slowly", lowLight: lowLight)
        }
        return QualityResult(accept: true, reason: lowLight ? "Too dark: add light" : nil, lowLight: lowLight)
    }

    /// Variance of the 3×3 Laplacian over a greyscale image (row-major, `width` × `height`, 0...255).
    static func laplacianVariance(_ px: [UInt8], width: Int, height: Int) -> Double {
        guard width > 2, height > 2 else { return 0 }
        var sum = 0.0, sumSq = 0.0
        for y in 1..<(height - 1) {
            let row = y * width
            for x in 1..<(width - 1) {
                let lap = Double(px[row - width + x]) + Double(px[row + width + x]) + Double(px[row + x - 1])
                    + Double(px[row + x + 1]) - 4 * Double(px[row + x])
                sum += lap
                sumSq += lap * lap
            }
        }
        let n = Double((width - 2) * (height - 2))
        let mean = sum / n
        return max(0, sumSq / n - mean * mean)
    }
}

/// Running median over the last `capacity` values (small, so a sort is fine).
struct RunningMedian: Sendable {
    let capacity: Int
    /// Samples needed before a median is reported.
    let minCount: Int
    private(set) var values: [Double] = []

    init(capacity: Int = 40, minCount: Int = 8) {
        self.capacity = capacity
        self.minCount = minCount
    }

    mutating func add(_ v: Double) {
        values.append(v)
        if values.count > capacity { values.removeFirst() }
    }

    var median: Double? {
        guard values.count >= minCount else { return nil }
        let s = values.sorted()
        return s[s.count / 2]
    }
}

/// Automatic torch decision (code decides; the device layer applies it). On at `onLevel` after `onAfter` seconds
/// of dim light (ambient or mean luma), off after `offAfter` seconds of good light; glare on the object steps the
/// level down (0.5 → 0.3 → 0.15 → off); heat caps it at 0.3.
struct TorchRule: Sendable, Equatable {
    enum Mode: String, Codable, Sendable { case auto, on, off }

    static let onAmbient = 400.0, offAmbient = 650.0
    static let onLuma = 0.25, offLuma = 0.35
    static let onAfter: TimeInterval = 1, offAfter: TimeInterval = 2
    static let levels: [Float] = [0.5, 0.3, 0.15]
    static let glareFraction = 0.03
    static let hotCap: Float = 0.3

    var mode: Mode = .auto
    private(set) var level: Float = 0          // 0 = off
    private var dimSince: TimeInterval?
    private var brightSince: TimeInterval?
    private var step = 0                        // index into levels
    private var onAt: TimeInterval?
    /// Brightness measured with the torch on (it lights the scene itself): switching off needs clearly more than this,
    /// or the torch would flicker off and on in a dim room.
    private var litAmbient: Double?
    private var litLuma: Double?
    static let settle: TimeInterval = 0.7
    static let glareStepEvery: TimeInterval = 0.5
    static let glareHold: TimeInterval = 10
    private var lastGlareStep: TimeInterval?
    /// After glare forced the torch off, auto does not turn it back on until then.
    private var holdOffUntil: TimeInterval?
    /// Why the last change happened, for the diagnostics log.
    private(set) var lastChange: String?
    static let brighterThanLit = 1.5

    /// Feeds one measurement; returns the level to apply (0 = off).
    /// - Parameters:
    ///   - ambient: ARKit ambient intensity (nil when unknown). With the torch on it includes the torch's light,
    ///     so "bright enough to switch off" also needs time.
    ///   - saturated: fraction of the object's pixels at luma > 0.97.
    /// - Parameter isoHigh: the fast-shutter exposure needs high ISO (noisy stills): counts as dim, so the torch
    ///   comes on sooner, and keeps it from switching off.
    mutating func update(time t: TimeInterval, ambient: Double?, meanLuma: Double, saturated: Double, hot: Bool,
                         isoHigh: Bool = false) -> Float {
        switch mode {
        case .off:
            level = 0
            return level
        case .on:
            level = hot ? min(Self.levels[0], Self.hotCap) : Self.levels[0]
            return level
        case .auto:
            break
        }
        let dim = (ambient.map { $0 < Self.onAmbient } ?? false) || meanLuma < Self.onLuma || isoHigh
        let bright = (ambient.map { $0 > Self.offAmbient } ?? true) && meanLuma > Self.offLuma && !isoHigh
        if level == 0 {
            brightSince = nil
            if dim, holdOffUntil.map({ t >= $0 }) ?? true {
                if dimSince == nil { dimSince = t }
                if t - dimSince! >= Self.onAfter {
                    step = 0
                    level = Self.levels[0]
                    dimSince = nil
                    onAt = t
                    litAmbient = nil
                    litLuma = nil
                    lastChange = "dim"
                }
            } else {
                dimSince = nil
            }
        } else {
            dimSince = nil
            if let onAt, litLuma == nil, t - onAt >= Self.settle {
                litAmbient = ambient
                litLuma = meanLuma
            }
            let brighterThanTorch = litLuma.map { lit in
                if let a = ambient, let la = litAmbient { return a > la * Self.brighterThanLit }
                return meanLuma > lit * Self.brighterThanLit
            } ?? false
            if saturated > Self.glareFraction, lastGlareStep.map({ t - $0 >= Self.glareStepEvery }) ?? true {
                step += 1
                lastGlareStep = t
                level = step < Self.levels.count ? Self.levels[step] : 0
                if level == 0 { holdOffUntil = t + Self.glareHold }
                lastChange = "glare"
                brightSince = nil
            } else if bright && brighterThanTorch {
                if brightSince == nil { brightSince = t }
                if t - brightSince! >= Self.offAfter {
                    level = 0
                    brightSince = nil
                    lastChange = "bright"
                }
            } else {
                brightSince = nil
            }
        }
        if hot, level > Self.hotCap { level = Self.hotCap }
        return level
    }
}

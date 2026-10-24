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

/// Automatic torch decision (code decides; the device layer applies it). On after `onAfter` seconds of dim light
/// (ambient, mean luma or high ISO), off after `offAfter` seconds of good light. Its level follows the subject
/// distance: `levels[0]` from afar, full (`nearLevel`) when the object is under `nearDistance` (close-ups and the
/// orbit of small objects), where the light falls off fastest and noise costs the most. Glare on the object (its
/// own projected box, never the whole frame) steps the level down one notch of `ladder` at a time down to off; ten
/// seconds without glare lets it back up a notch. Heat caps it at 0.3.
struct TorchRule: Sendable, Equatable {
    enum Mode: String, Codable, Sendable { case auto, on, off }

    static let onAmbient = 400.0, offAmbient = 650.0
    static let onLuma = 0.25, offLuma = 0.35
    static let onAfter: TimeInterval = 1, offAfter: TimeInterval = 2
    /// The level from afar, then the glare steps below it.
    static let levels: [Float] = [0.5, 0.3, 0.15]
    static let nearLevel: Float = 1.0
    /// Under this distance (m) the torch runs at `nearLevel`.
    static let nearDistance: Float = 0.30
    /// Every level, brightest first; glare steps down this ladder, a quiet spell steps back up.
    static let ladder: [Float] = [1.0, 0.7, 0.5, 0.3, 0.15]
    static let glareFraction = 0.03
    static let hotCap: Float = 0.3

    var mode: Mode = .auto
    private(set) var level: Float = 0          // 0 = off
    /// The highest level glare allows right now (1 = no limit).
    private(set) var cap: Float = 1
    private var dimSince: TimeInterval?
    private var brightSince: TimeInterval?
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
    ///   - saturated: fraction of the object's pixels at luma > 0.97 (inside its projected box).
    ///   - isoHigh: the fast-shutter exposure needs high ISO (noisy stills): counts as dim, so the torch comes on
    ///     sooner, and keeps it from switching off.
    ///   - near: the object is under `nearDistance`: full level.
    ///   - noisy: ISO is above the clean range; up close that alone turns the torch on (it is cheap light there).
    mutating func update(time t: TimeInterval, ambient: Double?, meanLuma: Double, saturated: Double, hot: Bool,
                         isoHigh: Bool = false, near: Bool = false, noisy: Bool = false) -> Float {
        let base = near ? Self.nearLevel : Self.levels[0]
        switch mode {
        case .off:
            level = 0
            return level
        case .on:
            level = hot ? min(base, Self.hotCap) : base
            return level
        case .auto:
            break
        }
        let wantsLight = isoHigh || (near && noisy)
        let dim = (ambient.map { $0 < Self.onAmbient } ?? false) || meanLuma < Self.onLuma || wantsLight
        let bright = (ambient.map { $0 > Self.offAmbient } ?? true) && meanLuma > Self.offLuma && !wantsLight
        if level == 0 {
            brightSince = nil
            if dim, holdOffUntil.map({ t >= $0 }) ?? true {
                if dimSince == nil { dimSince = t }
                if t - dimSince! >= Self.onAfter {
                    cap = 1
                    level = base
                    dimSince = nil
                    onAt = t
                    litAmbient = nil
                    litLuma = nil
                    lastGlareStep = nil
                    lastChange = near ? "dim_near" : "dim"
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
                // One notch below what is lit now.
                cap = Self.ladder.first { $0 < level - 1e-4 } ?? 0
                lastGlareStep = t
                level = cap
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
                if cap < 1, let g = lastGlareStep, t - g >= Self.glareHold, saturated < Self.glareFraction / 2 {
                    cap = Self.ladder.last { $0 > cap + 1e-4 } ?? 1
                    lastGlareStep = t
                }
                let want = min(base, cap)
                if want != level, want > 0 {
                    level = want
                    lastChange = near ? "near" : "far"
                }
            }
        }
        if hot, level > Self.hotCap { level = Self.hotCap }
        return level
    }
}

/// How big and how sharp the object must be in a photo for a real inspection (pure, unit-tested). Resolution is
/// measured as pixels per millimetre on the object (the still's focal length in pixels over the distance); framing
/// as the object's projected box against the frame's shorter side.
enum DetailTarget {
    /// Objects whose longest side is under this (m) are "small": shot from close and given close-ups of every side.
    static let smallObject: Float = 0.20
    /// An orbit still needs the object's projected box to span this share of the frame's shorter side.
    static let orbitFill: Float = 0.5
    /// Orbit stills of small objects: no farther than this (m).
    static let smallOrbitFar: Float = 0.25
    static let orbitNear: Float = 0.15
    /// Resolution that counts as "detail captured".
    static let orbitPxPerMm: Float = 8
    static let closePxPerMm: Float = 15
    /// Close-ups span this much beyond the nearest sharp distance (m).
    static let closeSpan: Float = 0.06
    /// Without the camera's own figure, the closest it focuses (m).
    static let defaultMinFocus: Float = 0.12
    /// In-box sharpness (Laplacian variance of 2 × 2 binned luma) below this is never kept, whatever the median.
    static let sharpnessFloor = 30.0

    static func isSmall(_ box: ObjectBox) -> Bool { max(box.size.x, box.size.y, box.size.z) < smallObject }

    static func pxPerMm(fx: Float, distance: Float) -> Float { distance > 0 ? fx / (distance * 1000) : 0 }

    /// The nearest distance the camera focuses, with a 5 % margin.
    static func nearestSharp(minFocus: Float?) -> Float { (minFocus ?? defaultMinFocus) * 1.05 }

    /// Close-up distances: from the nearest sharp distance out by `closeSpan` (small objects); larger objects keep
    /// a 15–25 cm band, never nearer than the camera focuses.
    static func closeBand(box: ObjectBox?, minFocus: Float?) -> ClosedRange<Float> {
        let near = nearestSharp(minFocus: minFocus)
        if let box, isSmall(box) {
            let lo = max(0.10, near)
            return lo...(lo + closeSpan)
        }
        let lo = max(0.15, near)
        return lo...max(lo + closeSpan, 0.25)
    }

    /// Orbit distances: small objects from the nearest sharp distance (≥ 15 cm) to 25 cm; larger ones out to the
    /// coverage band's far end.
    static func orbitBand(box: ObjectBox, minFocus: Float?) -> ClosedRange<Float> {
        let near = max(orbitNear, nearestSharp(minFocus: minFocus))
        if isSmall(box) { return near...max(near + 0.04, smallOrbitFar) }
        return near...max(near + 0.05, CoverageDome.band(box).upperBound)
    }

    /// Orbit resolution target: `orbitPxPerMm`, or what the object spanning `orbitFill` of the shorter side gives
    /// when that is less (a large object cannot reach 8 px/mm in one photo of all of it).
    static func orbitTarget(box: ObjectBox, shortSidePx: Float) -> Float {
        let longest = max(box.size.x, box.size.y, box.size.z) * 1000
        guard longest > 0 else { return orbitPxPerMm }
        return min(orbitPxPerMm, orbitFill * shortSidePx / longest)
    }

    /// Close-up resolution target: `closePxPerMm`, or 90 % of what this camera gives at its nearest sharp distance
    /// when that is less (a camera that cannot focus close enough must still be able to finish).
    static func closeTarget(fx: Float, minFocus: Float?) -> Float {
        min(closePxPerMm, 0.9 * pxPerMm(fx: fx, distance: nearestSharp(minFocus: minFocus)))
    }

    /// Where to stand for an orbit still, from here: the projected size grows as 1/distance, so the distance that
    /// gives the fill target with 10 % to spare, kept inside the band. Metres.
    static func approachDistance(current d: Float, fill: Float, band: ClosedRange<Float>) -> Float {
        let byFill = fill > 0 ? d * fill / (orbitFill * 1.1) : band.lowerBound
        let inBand = min(band.upperBound * 0.9, byFill)
        return max(band.lowerBound, min(inBand, d))
    }

    /// The on-screen cue for "come closer", with the distance to aim for.
    static func approachCue(current d: Float, fill: Float, band: ClosedRange<Float>) -> String {
        let cm = Int((approachDistance(current: d, fill: fill, band: band) * 100).rounded())
        return "Move closer: about \(cm) cm away"
    }
}

import ARKit
import AVFoundation

/// Fast shutter for sharp stills while walking: a custom exposure on the AR camera
/// (`configurableCaptureDeviceForPrimaryCamera`) at `maxDuration` while the phone moves, with ISO steered from the
/// camera's own metering offset (`exposureTargetOffset`, in EV) a few times a second. When the phone is nearly
/// still, `adapt` lengthens the shutter up to `steadyMaxDuration` (as far as the measured motion keeps the blur
/// under `blurBudgetPx`) and lowers ISO by the same factor, so the brightness stays and the noise drops (1/100 s =
/// ISO ÷ 2.5). Auto exposure is restored on exit. Every change and every refusal goes to diag, so what ARKit really
/// allows is measured, not assumed.
@MainActor
final class ExposureControl {
    static let maxDuration = CMTime(value: 1, timescale: 250)
    /// The longest shutter while the phone is nearly still.
    static let steadyMaxDuration = CMTime(value: 1, timescale: 100)
    /// Predicted motion blur (still pixels) the steady shutter may add.
    static let blurBudgetPx = 1.5
    /// A new shutter is in the frames after this long (a few frames at 30 fps).
    static let settleTime: TimeInterval = 0.12
    /// No longer shutter once ISO is already this clean (nothing to gain, only blur to risk).
    static let cleanISO: Float = 200
    /// ISO above this is noisy enough that light close to the object is worth it.
    static let noisyISO: Float = 800
    /// ISO above this share of the sensor's maximum counts as "high": the torch should help.
    static let highISOShare: Float = 0.6
    static let deadband: Float = 0.25          // EV
    static let interval: TimeInterval = 0.3
    /// When the shutter last changed (frame time); stills wait `settleTime` after it.
    private(set) var changedAt: TimeInterval = -.infinity
    private(set) var steady = false

    private(set) var iso: Float = 0
    private(set) var duration: Double = 0
    private(set) var maxISO: Float = 0
    private(set) var active = false
    private var lastAdjust: TimeInterval = 0
    private let diag: Diag

    init(diag: Diag) { self.diag = diag }

    var isoHigh: Bool { active && maxISO > 0 && iso >= maxISO * Self.highISOShare }
    var noisy: Bool { active && iso > Self.noisyISO }

    /// The shutter in use has reached the frames.
    func settled(_ t: TimeInterval) -> Bool { t - changedAt >= Self.settleTime }

    /// Picks the shutter for this moment: `maxDuration` while moving; when nearly still, as long as the blur budget
    /// allows (`blurRate` = predicted image motion in still pixels per second), up to `steadyMaxDuration`, and only
    /// while ISO is above `cleanISO`. ISO changes by the inverse factor (same brightness). Call at ~10 Hz.
    func adapt(time t: TimeInterval, steady still: Bool, blurRate: Double?) {
        guard active, let d = device, duration > 0 else { return }
        let f = d.activeFormat
        let fast = max(f.minExposureDuration.seconds, min(Self.maxDuration.seconds, f.maxExposureDuration.seconds))
        var want = fast
        if still {
            let longest = min(Self.steadyMaxDuration.seconds, f.maxExposureDuration.seconds)
            let byBlur = blurRate.map { $0 > 0 ? Self.blurBudgetPx / $0 : longest } ?? longest
            want = max(fast, min(longest, byBlur))
            // Stop lengthening once ISO would fall under the clean level.
            let isoThere = iso * Float(duration / want)
            if isoThere < Self.cleanISO { want = max(fast, min(want, duration * Double(iso / Self.cleanISO))) }
        }
        steady = still
        guard abs(want - duration) / duration > 0.15 else { return }
        // Shorter at once (the phone moves); longer at most every `interval` (no hunting on a noisy motion reading).
        if want > duration, t - changedAt < Self.interval { return }
        let ratio = Float(duration / want)
        set(duration: CMTime(seconds: want, preferredTimescale: 1_000_000), iso: iso * ratio, why: still ? "steady" : "moving",
            extra: ["blur_rate_px_s": blurRate.map { ($0 * 10).rounded() / 10 } ?? -1])
        changedAt = t
    }

    private var device: AVCaptureDevice? { ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera }

    /// Switches to the capped custom exposure, starting from the ISO that keeps auto exposure's brightness.
    func start() {
        guard let d = device else {
            diag.log("exposure_unavailable", ["reason": "no configurable AR camera"])
            return
        }
        let f = d.activeFormat
        let autoDur = d.exposureDuration.seconds, autoISO = d.iso
        let dur = CMTimeMaximum(f.minExposureDuration, CMTimeMinimum(Self.maxDuration, f.maxExposureDuration))
        // Same brightness: ISO × duration stays constant.
        let target = autoDur > 0 ? autoISO * Float(autoDur / dur.seconds) : autoISO
        maxISO = f.maxISO
        set(duration: dur, iso: target, why: "start", extra: ["auto_duration_s": autoDur, "auto_iso": autoISO,
                                                              "min_iso": f.minISO, "max_iso": f.maxISO,
                                                              "min_duration_s": f.minExposureDuration.seconds])
    }

    /// Re-meters: brighter or darker by the offset the camera reports. Call at ~10 Hz; acts at most every 0.3 s.
    func update(time t: TimeInterval) {
        guard active, t - lastAdjust >= Self.interval, let d = device else { return }
        if d.exposureMode != .custom {
            // ARKit (or the system) took exposure back: re-apply, and log it so we know it happens.
            diag.log("exposure_reset_by_system", ["mode": d.exposureMode.rawValue])
            set(duration: CMTime(seconds: duration, preferredTimescale: 1_000_000), iso: iso, why: "reapply")
            return
        }
        let offset = d.exposureTargetOffset
        guard abs(offset) > Self.deadband else { return }
        lastAdjust = t
        // Negative offset = darker than target → raise ISO by 2^(−offset), damped by half a step.
        set(duration: CMTime(seconds: duration, preferredTimescale: 1_000_000), iso: iso * pow(2, -offset * 0.5), why: "meter",
            extra: ["offset_ev": offset])
    }

    func stop() {
        guard active, let d = device else { return }
        do {
            try d.lockForConfiguration()
            d.exposureMode = .continuousAutoExposure
            d.unlockForConfiguration()
            diag.log("exposure_restored")
        } catch {
            diag.log("exposure_error", ["where": "restore", "error": error.localizedDescription])
        }
        active = false
    }

    private func set(duration dur: CMTime, iso target: Float, why: String, extra: [String: Any] = [:]) {
        guard let d = device else { return }
        let iso = min(d.activeFormat.maxISO, max(d.activeFormat.minISO, target))
        do {
            try d.lockForConfiguration()
            d.setExposureModeCustom(duration: dur, iso: iso, completionHandler: nil)
            d.unlockForConfiguration()
            let changed = abs(iso - self.iso) > 1 || !active
            self.iso = iso
            duration = dur.seconds
            active = true
            if changed || why != "meter" {
                diag.log("exposure", extra.merging(["why": why, "duration_s": dur.seconds, "iso": iso,
                                                    "iso_high": isoHigh]) { a, _ in a })
            }
        } catch {
            diag.log("exposure_error", ["where": why, "error": error.localizedDescription])
        }
    }
}

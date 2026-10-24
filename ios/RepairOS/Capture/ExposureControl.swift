import ARKit
import AVFoundation

/// Fast shutter for sharp stills while walking: a custom exposure on the AR camera
/// (`configurableCaptureDeviceForPrimaryCamera`) capped at `maxDuration`, with ISO steered from the camera's own
/// metering offset (`exposureTargetOffset`, in EV) a few times a second. Auto exposure is restored on exit. Every
/// change and every refusal goes to diag, so what ARKit really allows is measured, not assumed.
@MainActor
final class ExposureControl {
    static let maxDuration = CMTime(value: 1, timescale: 250)
    /// ISO above this share of the sensor's maximum counts as "high": the torch should help.
    static let highISOShare: Float = 0.6
    static let deadband: Float = 0.25          // EV
    static let interval: TimeInterval = 0.3

    private(set) var iso: Float = 0
    private(set) var duration: Double = 0
    private(set) var maxISO: Float = 0
    private(set) var active = false
    private var lastAdjust: TimeInterval = 0
    private let diag: Diag

    init(diag: Diag) { self.diag = diag }

    var isoHigh: Bool { active && maxISO > 0 && iso >= maxISO * Self.highISOShare }

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

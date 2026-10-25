import ARKit
import AVFoundation
import UIKit

/// Applies `TorchRule` to the AR session's camera: `configurableCaptureDeviceForPrimaryCamera` →
/// `lockForConfiguration` → `setTorchModeOn(level:)` / `torchMode = .off`, while the session runs. Every change
/// (and every refusal) is logged.
@MainActor
final class Torch {
    private(set) var rule = TorchRule()
    private(set) var applied: Float = 0
    private(set) var unavailable: String?
    /// Battery under 15 %: ask once before the auto torch lights.
    private(set) var needsBatteryConsent = false
    private var batteryConsent: Bool?
    private let diag: Diag

    init(diag: Diag) {
        self.diag = diag
        rule.mode = TorchRule.Mode(rawValue: UserDefaults.standard.string(forKey: "torchMode") ?? "") ?? .auto
        UIDevice.current.isBatteryMonitoringEnabled = true
    }

    var mode: TorchRule.Mode { rule.mode }

    func setMode(_ m: TorchRule.Mode) {
        rule.mode = m
        UserDefaults.standard.set(m.rawValue, forKey: "torchMode")
        diag.log("torch_mode", ["mode": m.rawValue])
    }

    func answerBattery(_ yes: Bool) {
        batteryConsent = yes
        needsBatteryConsent = false
        diag.log("torch_battery_answer", ["use": yes])
    }

    /// The level the torch reaches when nothing holds it back (full up close, else the far level).
    func top(near: Bool) -> Float { near ? TorchRule.nearLevel : TorchRule.levels[0] }

    func update(time: TimeInterval, ambient: Double?, meanLuma: Double, saturated: Double, isoHigh: Bool = false,
                near: Bool = false, noisy: Bool = false) {
        let hot = ProcessInfo.processInfo.thermalState.rawValue >= ProcessInfo.ThermalState.serious.rawValue
        var level = rule.update(time: time, ambient: ambient, meanLuma: meanLuma, saturated: saturated, hot: hot, isoHigh: isoHigh,
                                near: near, noisy: noisy)
        if level > 0, rule.mode == .auto {
            let battery = UIDevice.current.batteryLevel
            if battery >= 0, battery < 0.15, UIDevice.current.batteryState != .charging {
                if batteryConsent == nil { needsBatteryConsent = true }
                if batteryConsent != true { level = 0 }
            }
        }
        apply(level, reason: rule.lastChange, ambient: ambient, luma: meanLuma, saturated: saturated)
    }

    func off() { apply(0, reason: "end", ambient: nil, luma: nil, saturated: nil) }

    private func apply(_ level: Float, reason: String?, ambient: Double?, luma: Double?, saturated: Double?) {
        guard level != applied else { return }
        guard let device = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera, device.hasTorch else {
            if unavailable == nil {
                unavailable = "no torch on the AR camera"
                diag.log("torch_unavailable", ["reason": unavailable!])
            }
            return
        }
        do {
            try device.lockForConfiguration()
            if level > 0 {
                try device.setTorchModeOn(level: min(level, AVCaptureDevice.maxAvailableTorchLevel))
            } else {
                device.torchMode = .off
            }
            device.unlockForConfiguration()
            applied = level
            diag.log("torch", ["level": level, "reason": reason ?? rule.mode.rawValue, "ambient": ambient ?? -1, "cap": rule.cap,
                               "luma": luma ?? -1, "saturated": saturated ?? -1, "torch_active": device.isTorchActive])
        } catch {
            unavailable = error.localizedDescription
            diag.log("torch_error", ["level": level, "error": error.localizedDescription])
        }
    }
}

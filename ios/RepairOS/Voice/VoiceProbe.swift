import Foundation

/// Developer hook (`--voice-frame-probe`, unattended runs only): while the scanner is open, Lea says one fixed
/// cue line every ~4.5 s (the fastest the pacing rule allows) and the display frame time of the last 5 s goes to
/// diag.jsonl as `voice_frame_probe`, so the 60 fps budget can be compared with the voice on and muted.
@MainActor
enum VoiceProbe {
    static var enabled: Bool { CommandLine.arguments.contains("--voice-frame-probe") }

    static func run(scan: ScanSession) async {
        guard enabled else { return }
        let lines = VoiceCues.fixedLines
        var i = 0
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(4.5))
            if Task.isCancelled { return }
            LeaVoice.shared.dynamicCue(lines[i % lines.count], diag: scan.diag)
            i += 1
            guard let times = scan.renderer?.frameTimes.suffix(300), !times.isEmpty else { continue }
            let s = times.sorted()
            scan.diag.log("voice_frame_probe", [
                "muted": LeaVoice.shared.muted, "wake": WakeListener.shared.mode != .off, "n": times.count,
                "mean_ms": (times.reduce(0, +) / Double(times.count) * 10).rounded() / 10,
                "p95_ms": (s[Int(Double(s.count - 1) * 0.95)] * 10).rounded() / 10,
                "over_20ms": times.filter { $0 > 20 }.count])
        }
    }
}

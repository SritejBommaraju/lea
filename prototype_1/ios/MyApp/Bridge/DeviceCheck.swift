import AVFoundation
import SwiftUI

/// Runs the camera and the microphone together for five seconds and reports what arrived from each, so the
/// combination Lea relies on (coaching or zoom while talking) is checked on this iPhone in one tap.
struct DeviceCheckRow: View {
    @EnvironmentObject private var conversation: VoiceCoordinator
    @State private var result: String?
    @State private var running = false

    var body: some View {
        Section("Device check") {
            Button(running ? "Checking…" : "Check camera and microphone together") { Task { await run() } }
                .disabled(running || conversation.state != .idle || conversation.handsFree || conversation.wakeOn)
            if conversation.state != .idle || conversation.handsFree || conversation.wakeOn {
                Text("Turn off hands-free and “Hey Leah” first; the check needs the microphone to itself.").font(.caption)
            }
            if let result { Text(result).font(.caption) }
        }
    }

    private func run() async {
        running = true
        defer { running = false }
        let camera = CameraService()
        camera.start()
        let engine = AVAudioEngine()
        let seconds = Locked(0.0)
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker])
            try session.setActive(true)
            let format = engine.inputNode.outputFormat(forBus: 0)
            engine.inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
                seconds.value += Double(buffer.frameLength) / format.sampleRate
            }
            try engine.start()
        } catch {
            result = "Microphone did not start: \(error.localizedDescription)"
            camera.stop()
            return
        }
        var frames = 0
        let end = Date().addingTimeInterval(5)
        while Date() < end {
            if await camera.liveFrame() != nil { frames += 1 }
            try? await Task.sleep(for: .milliseconds(250))
        }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        camera.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        let audio = seconds.value
        let ok = frames >= 10 && audio >= 4
        result = "\(ok ? "Passed" : "Failed"): \(frames) camera frames and \(audio.formatted(.number.precision(.fractionLength(1)))) s of audio in 5 s."
    }
}

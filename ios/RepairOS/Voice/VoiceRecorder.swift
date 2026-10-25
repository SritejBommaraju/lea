@preconcurrency import AVFoundation
import Foundation
@preconcurrency import Speech

/// Hold-to-talk recording (AAC in .m4a, 16 kHz mono: small to upload) and the phone's own transcription when the
/// gateway has no voice key (SFSpeechRecognizer, on the device when it can).
@MainActor
final class VoiceRecorder {
    private var recorder: AVAudioRecorder?
    private(set) var startedAt: Date?
    let file = URL.temporaryDirectory.appending(path: "lea-hold-to-talk.m4a")

    /// Mic permission, asked once.
    static func allowed() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return true
        case .denied: return false
        default: return await AVAudioApplication.requestRecordPermission()
        }
    }

    func start() async -> Bool {
        guard await Self.allowed(), await VoiceAudioSession.record() else { return false }
        LeaVoice.shared.stop()
        try? FileManager.default.removeItem(at: file)
        let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
                                       AVNumberOfChannelsKey: 1, AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue]
        guard let r = try? AVAudioRecorder(url: file, settings: settings), r.record() else {
            VoiceAudioSession.playback()
            return false
        }
        recorder = r
        startedAt = Date()
        return true
    }

    /// Stops and returns the recording, or nil when it was too short to hold a word.
    func stop() -> Data? {
        guard let r = recorder else { return nil }
        let length = r.currentTime
        r.stop()
        recorder = nil
        VoiceAudioSession.playback()
        guard length >= 0.35 else { return nil }
        return try? Data(contentsOf: file)
    }

    func cancel() {
        recorder?.stop()
        recorder = nil
        VoiceAudioSession.playback()
    }

    /// The phone's own transcription of the last recording ("" when nothing was understood).
    func transcribeOnDevice() async -> String {
        guard await Self.speechAllowed(), let rec = SFSpeechRecognizer(locale: Locale(identifier: "en-US")), rec.isAvailable else {
            return ""
        }
        let request = SFSpeechURLRecognitionRequest(url: file)
        request.shouldReportPartialResults = false
        if rec.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        return await withCheckedContinuation { (c: CheckedContinuation<String, Never>) in
            let once = Once()
            rec.recognitionTask(with: request) { @Sendable result, error in
                if let result, result.isFinal {
                    once.run { c.resume(returning: result.bestTranscription.formattedString) }
                } else if error != nil {
                    once.run { c.resume(returning: "") }
                }
            }
        }
    }

    private static func speechAllowed() async -> Bool { await SpeechPermission.request() }
}

/// Speech recognition permission. Not on the main actor: the system answers on a background queue, and a
/// main-actor closure called there traps (seen on the phone).
enum SpeechPermission {
    static func request() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return true
        case .notDetermined:
            return await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
                SFSpeechRecognizer.requestAuthorization { @Sendable status in c.resume(returning: status == .authorized) }
            }
        default: return false
        }
    }
}

/// Runs its block once (a recognition callback can fire more than once).
private final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    func run(_ f: () -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard !done else { return }
        done = true
        f()
    }
}

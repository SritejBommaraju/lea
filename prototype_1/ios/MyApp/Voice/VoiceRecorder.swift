import AVFoundation
import Combine
import Foundation

@MainActor
final class VoiceRecorder: NSObject, ObservableObject, AVAudioRecorderDelegate {
    @Published private(set) var isRecording = false
    @Published var message: String?
    var onFinished: ((URL, TimeInterval) -> Void)?

    private var recorder: AVAudioRecorder?
    private var startedAt: Date?

    func toggle() {
        if isRecording { stop() }
        else { requestAndStart() }
    }

    func stop() {
        guard let recorder else { return }
        recorder.stop()
        self.recorder = nil
        isRecording = false
        let duration = Date().timeIntervalSince(startedAt ?? Date())
        startedAt = nil
        if duration >= 0.2 { onFinished?(recorder.url, duration) }
        else { try? FileManager.default.removeItem(at: recorder.url) }
    }

    private func requestAndStart() {
        AVAudioApplication.requestRecordPermission { [weak self] allowed in
            DispatchQueue.main.async {
                if allowed { self?.startRecording() }
                else { self?.message = "Microphone access is needed for voice notes. Enable it in Settings." }
            }
        }
    }

    private func startRecording() {
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
            try audioSession.setActive(true)
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("lea-\(UUID().uuidString).m4a")
            recorder = try AVAudioRecorder(url: url, settings: [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 44_100,
                AVNumberOfChannelsKey: 1,
                AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue
            ])
            recorder?.delegate = self
            guard recorder?.record() == true else { throw RecorderError.couldNotStart }
            startedAt = Date()
            isRecording = true
        } catch {
            message = "Recording could not start: \(error.localizedDescription)"
            recorder = nil
        }
    }

    private enum RecorderError: LocalizedError {
        case couldNotStart
        var errorDescription: String? { "The microphone did not start recording." }
    }
}

import AVFoundation
import CoreML
import SoundAnalysis

/// "Hey Leah" through Apple's SoundAnalysis with a custom sound classifier (Create ML), trained by
/// LeaServices/wake/train.swift on the owner's recordings. Active only when HeyLeah.mlmodelc is in the app bundle;
/// until then Lea offers push-to-talk and hands-free. Runs on the audio thread, entirely on the iPhone.
nonisolated final class SoundWakeDetector: WakeWordDetector, @unchecked Sendable {
    let name = "Hey Leah (on-device sound classifier)"
    static let label = "hey_leah"
    static let threshold = 0.85

    private let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
    private let analyzer: SNAudioStreamAnalyzer
    private let observer = Observer()
    private var position: AVAudioFramePosition = 0

    /// nil when no trained model is bundled or it can't be loaded.
    init?() {
        guard let url = Bundle.main.url(forResource: "HeyLeah", withExtension: "mlmodelc"),
              let model = try? MLModel(contentsOf: url),
              let request = try? SNClassifySoundRequest(mlModel: model) else { return nil }
        request.windowDuration = CMTime(seconds: 1.0, preferredTimescale: 16_000)
        request.overlapFactor = 0.5
        analyzer = SNAudioStreamAnalyzer(format: format)
        do { try analyzer.add(request, withObserver: observer) } catch { return nil }
    }

    func process(_ samples: [Int16]) -> Bool {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { return false }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        for (i, s) in samples.enumerated() { buffer.floatChannelData![0][i] = Float(s) / 32768 }
        analyzer.analyze(buffer, atAudioFramePosition: position)
        position += AVAudioFramePosition(samples.count)
        return observer.take()
    }

    private final class Observer: NSObject, SNResultsObserving, @unchecked Sendable {
        private let lock = NSLock()
        private var heard = false

        func request(_ request: SNRequest, didProduce result: SNResult) {
            guard let r = result as? SNClassificationResult,
                  let c = r.classification(forIdentifier: SoundWakeDetector.label), c.confidence >= SoundWakeDetector.threshold else { return }
            lock.withLock { heard = true }
        }

        /// The detection since the last call, consumed once so one phrase opens one turn.
        func take() -> Bool { lock.withLock { defer { heard = false }; return heard } }
    }
}

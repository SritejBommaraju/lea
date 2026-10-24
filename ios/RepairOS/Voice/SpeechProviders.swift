import AVFoundation
import Foundation
import Speech

/// One event vocabulary for every recognizer, after VEL's vel-stt protocol (workers/vel-stt/PROTOCOL.md):
/// speech starts, partial words, then exactly one of transcript / noSpeech / error per utterance.
nonisolated enum SpeechEvent: Sendable {
    case speechStart
    case partial(String)
    case transcript(String)
    case noSpeech
    case error(String)
}

/// A recognizer Lea can listen through. Audio arrives from the one microphone owner (VoiceCoordinator).
@MainActor
protocol SpeechInput: AnyObject {
    var name: String { get }
    var onEvent: ((SpeechEvent) -> Void)? { get set }
    /// hold: ends only on `finish()` (push-to-talk). open: ends on a pause judged by VEL's end-of-turn rules.
    func begin(open: Bool) throws
    /// Microphone audio in the hardware format (Apple's recognizer reads this).
    nonisolated func feed(native buffer: AVAudioPCMBuffer)
    /// The same audio as 16 kHz mono Int16, in 100 ms chunks (Cartesia reads this).
    nonisolated func feed(pcm16k chunk: Data)
    func finish()
    func cancel()
}

/// A voice Lea speaks through. `stop()` returns the share of the current sentence already heard (0...1).
@MainActor
protocol SpeechOutput: AnyObject {
    var name: String { get }
    func speak(_ sentence: String) async
    func stop() -> Double
}

// MARK: - Apple on-device recognition

/// SFSpeechRecognizer with on-device recognition (no audio leaves the phone), and VEL's end-of-turn timing: after
/// each partial result, the pause that ends the turn depends on whether the words sound finished.
@MainActor
final class AppleSpeechInput: SpeechInput {
    var onEvent: ((SpeechEvent) -> Void)?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    /// On-device where the iPhone supports it (no audio leaves the phone); older iPhones use Apple's server.
    var name: String { recognizer?.supportsOnDeviceRecognition == true ? "Apple on-device" : "Apple (server)" }
    private let box = RequestBox()
    private var task: SFSpeechRecognitionTask?
    private var endTimer: Task<Void, Never>?
    private var latest = ""
    private var started = false
    private var open = false
    private var done = false
    private var generation = 0                       // bumped by each begin and cancel: older callbacks are ignored

    static func authorize() async -> Bool {
        await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0 == .authorized) } }
    }

    func begin(open: Bool) throws {
        guard let recognizer, recognizer.isAvailable else {
            throw NSError(domain: "Lea", code: 1, userInfo: [NSLocalizedDescriptionKey: "Speech recognition is not available right now."])
        }
        cancel()
        self.open = open
        latest = ""; started = false; done = false
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        request.shouldReportPartialResults = true
        request.addsPunctuation = true                  // the end-of-turn rules read punctuation, as Parakeet's drafts have
        request.contextualStrings = ["Leah"]   // no part vocabulary: Lea works on any object
        box.request = request
        generation += 1
        let mine = generation
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let final = result?.isFinal == true
            Task { @MainActor in
                guard let self, self.generation == mine else { return }
                self.handle(text: text, final: final, error: error)
            }
        }
    }

    nonisolated func feed(native buffer: AVAudioPCMBuffer) { box.request?.append(buffer) }
    nonisolated func feed(pcm16k chunk: Data) {}

    func finish() {
        endTimer?.cancel()
        box.request?.endAudio()
    }

    /// Cancelled requests are finished: words heard before the cancel never become a turn.
    func cancel() {
        done = true
        generation += 1
        endTimer?.cancel()
        task?.cancel()
        task = nil
        box.request = nil
    }

    private func handle(text: String?, final: Bool, error: Error?) {
        guard !done else { return }
        if let text, !text.isEmpty {
            if !started { started = true; onEvent?(.speechStart) }
            latest = text
            onEvent?(.partial(text))
            if open {
                endTimer?.cancel()
                let wait = TurnTaking.hangover(after: text)
                endTimer = Task { [weak self] in
                    try? await Task.sleep(for: .seconds(wait))
                    if !Task.isCancelled { self?.finish() }
                }
            }
        }
        if final || error != nil {
            done = true
            endTimer?.cancel()
            box.request = nil
            if !latest.trimmingCharacters(in: .whitespaces).isEmpty { onEvent?(.transcript(latest)) }
            else if let error, (error as NSError).code != 216 && (error as NSError).code != 1110 { onEvent?(.error(error.localizedDescription)) }   // 216: cancelled, 1110: no speech
            else { onEvent?(.noSpeech) }
        }
    }
}

/// The request is fed from the audio thread and replaced on the main actor.
nonisolated final class RequestBox: @unchecked Sendable {
    private let lock = NSLock()
    private var _request: SFSpeechAudioBufferRecognitionRequest?
    var request: SFSpeechAudioBufferRecognitionRequest? {
        get { lock.withLock { _request } }
        set { lock.withLock { _request = newValue } }
    }
}

// MARK: - Cartesia (when the bridge has a key)

@MainActor
final class CartesiaSpeechInput: SpeechInput {
    let name = "Cartesia Ink 2"
    var onEvent: ((SpeechEvent) -> Void)?
    private let grant: CartesiaGrant
    private let socket = Locked<CartesiaSTT?>(nil)
    private var started = false

    init(grant: CartesiaGrant) { self.grant = grant }

    func begin(open: Bool) throws {
        started = false
        let stt = CartesiaSTT(grant: grant)
        stt.onPartial = { [weak self] text in Task { @MainActor in
            guard let self else { return }
            if !self.started, !text.isEmpty { self.started = true; self.onEvent?(.speechStart) }
            self.onEvent?(.partial(text))
        } }
        stt.onFinal = { [weak self] text in Task { @MainActor in self?.onEvent?(text.isEmpty ? .noSpeech : .transcript(text)) } }
        stt.onError = { [weak self] text in Task { @MainActor in self?.onEvent?(.error(text)) } }
        try stt.connect()
        socket.value = stt
    }

    nonisolated func feed(native buffer: AVAudioPCMBuffer) {}
    nonisolated func feed(pcm16k chunk: Data) { socket.value?.send(chunk) }

    func finish() { socket.value?.finish() }
    func cancel() { socket.value?.cancel(); socket.value = nil }
}

nonisolated final class Locked<V>: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: V
    init(_ value: V) { _value = value }
    var value: V {
        get { lock.withLock { _value } }
        set { lock.withLock { _value = newValue } }
    }
}

// MARK: - Voices

/// The system voice: always available, starts at once (VEL uses it the same way when its own voice is off).
@MainActor
final class SystemVoiceOutput: NSObject, SpeechOutput, AVSpeechSynthesizerDelegate {
    let name = "System voice"
    private let synth = AVSpeechSynthesizer()
    private var continuation: CheckedContinuation<Void, Never>?
    private var spokenTo = 0
    private var length = 1
    private let voice = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("en") }.max { $0.quality.rawValue < $1.quality.rawValue }

    override init() {
        super.init()
        synth.delegate = self
    }

    func speak(_ sentence: String) async {
        let u = AVSpeechUtterance(string: TurnTaking.speakable(sentence))
        u.voice = voice
        u.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
        spokenTo = 0
        length = max(1, (u.speechString as NSString).length)
        await withCheckedContinuation { c in
            continuation = c
            synth.speak(u)
        }
    }

    func stop() -> Double {
        let heard = Double(spokenTo) / Double(length)
        synth.stopSpeaking(at: .word)
        resume()
        return heard
    }

    private func resume() { continuation?.resume(); continuation = nil }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, willSpeakRangeOfSpeechString range: NSRange, utterance: AVSpeechUtterance) {
        Task { @MainActor in self.spokenTo = range.location }
    }
    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) { Task { @MainActor in self.resume() } }
    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) { Task { @MainActor in self.resume() } }
}

/// Cartesia Sonic through the coordinator's audio engine.
@MainActor
final class CartesiaVoiceOutput: SpeechOutput {
    let name = "Cartesia Sonic"
    private let grant: CartesiaGrant
    private let player: AVAudioPlayerNode
    private let engine: AVAudioEngine
    private var current: Task<Void, Never>?
    private var scheduled = 0
    private var played = 0
    /// A sentence Cartesia couldn't say (socket dropped, token refused) is said by the iPhone voice instead.
    private var fallback: SystemVoiceOutput?
    private var stopped = false
    /// Told why Cartesia failed, so the person knows and later sentences can use the iPhone voice.
    private let onFailure: (String) -> Void

    init(grant: CartesiaGrant, engine: AVAudioEngine, player: AVAudioPlayerNode, onFailure: @escaping (String) -> Void) {
        self.grant = grant; self.engine = engine; self.player = player; self.onFailure = onFailure
    }

    func speak(_ sentence: String) async {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24_000, channels: 1, interleaved: false)!
        engine.connect(player, to: engine.mainMixerNode, format: format)
        if !engine.isRunning { try? engine.start() }
        player.play()
        scheduled = 0; played = 0
        stopped = false
        var failure: Error?
        let task = Task {
            do {
                for try await samples in CartesiaTTS(grant: grant).stream(TurnTaking.speakable(sentence)) {
                    guard !Task.isCancelled, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { break }
                    buffer.frameLength = AVAudioFrameCount(samples.count)
                    samples.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
                    scheduled += samples.count
                    await player.scheduleBuffer(buffer)
                    played += samples.count
                }
            } catch {
                failure = error
            }
        }
        current = task
        await task.value
        // Interrupted on purpose (barge-in, a newer turn): stay quiet. Otherwise say the sentence another way.
        guard let failure, !stopped, !Task.isCancelled, !(failure is CancellationError),
              (failure as? URLError)?.code != .cancelled else { return }
        onFailure(failure.localizedDescription)
        let voice = SystemVoiceOutput()
        fallback = voice
        await voice.speak(sentence)
        fallback = nil
    }

    func stop() -> Double {
        stopped = true
        if let fallback { return fallback.stop() }
        current?.cancel()
        player.stop()
        return scheduled > 0 ? Double(played) / Double(scheduled) : 0
    }
}

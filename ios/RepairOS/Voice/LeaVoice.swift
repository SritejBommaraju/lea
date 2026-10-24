@preconcurrency import AVFoundation
import CryptoKit
import Foundation
import Observation

/// Lea's voice: the report summary when a report lands, short scan cues, and spoken answers. Speech comes from the
/// gateway (Cartesia, streamed as PCM and played on the first chunk) when the backend has a key, else from the
/// phone (AVSpeechSynthesizer). One utterance at a time; mute is remembered.
@MainActor
@Observable
final class LeaVoice: NSObject {
    static let shared = LeaVoice()
    static let mutedKey = "leaVoiceMuted"
    private static let spokenKey = "leaVoiceSpokenReports"

    var muted: Bool {
        didSet {
            UserDefaults.standard.set(muted, forKey: Self.mutedKey)
            if muted { stop() }
        }
    }
    private(set) var speaking = false {
        didSet { if speaking != oldValue { WakeListener.shared.leaSpeaking(speaking) } }   // never hear herself
    }
    private(set) var status: VoiceStatus?
    /// "cartesia" or "device": what the last utterance used.
    private(set) var lastSource = ""

    @ObservationIgnored private weak var app: AppState?
    @ObservationIgnored private var player = PCMPlayer(sampleRate: 24000)
    @ObservationIgnored private let synth = AVSpeechSynthesizer()
    @ObservationIgnored private var cuePCM: [String: Data] = [:]
    @ObservationIgnored private var limiter = CueLimiter()
    @ObservationIgnored private var currentCue: String?
    /// The scan's diag.jsonl: every spoken cue is logged there (`voice_cue`).
    @ObservationIgnored private var cueDiag: Diag?
    @ObservationIgnored private var pendingCue: Task<Void, Never>?
    @ObservationIgnored private var utterance = 0
    @ObservationIgnored private var deviceStarted: (@MainActor @Sendable () -> Void)?
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    @ObservationIgnored private let clock = ContinuousClock()
    @ObservationIgnored private let t0 = ContinuousClock.now

    override init() {
        muted = UserDefaults.standard.bool(forKey: Self.mutedKey)
        super.init()
        synth.delegate = self
    }

    private var now: TimeInterval {
        let d = clock.now - t0
        return Double(d.components.seconds) + Double(d.components.attoseconds) * 1e-18
    }

    private var useCartesia: Bool { status?.cartesia == true && app?.gateway != nil && app?.isPaired == true }

    // MARK: - Start

    /// At launch: the audio session (once, off the main thread), what the gateway offers, then the fixed cue lines.
    func attach(_ app: AppState) {
        self.app = app
        // Developer hooks for unattended runs: `--voice-muted` / `--voice-unmuted` (remembered like the toggle).
        if CommandLine.arguments.contains("--voice-muted") { muted = true }
        if CommandLine.arguments.contains("--voice-unmuted") { muted = false }
        VoiceAudioSession.playback()
        // What this gateway offered last time, at once (a report can land before the status call answers); the
        // call below corrects it, and a failed stream falls back to the phone's voice anyway.
        if let saved = UserDefaults.standard.dictionary(forKey: Self.statusKey), saved["gateway"] as? String == app.gatewayURL {
            status = VoiceStatus(cartesia: saved["cartesia"] as? Bool ?? false, answers: saved["answers"] as? Bool ?? false,
                                 sampleRate: 24000, voiceID: saved["voice"] as? String)
        }
        Task { await refresh() }
        WakeListener.shared.refresh()
    }

    private static let statusKey = "leaVoiceStatus"

    /// Asks the gateway again (after pairing or a new address) and prepares the cue lines.
    func refresh() async {
        guard let app, app.isPaired, let gateway = app.gateway else { status = nil; return }
        guard let s = try? await gateway.voiceStatus() else { return }   // offline: keep what we knew
        status = s
        UserDefaults.standard.set(["gateway": app.gatewayURL, "cartesia": s.cartesia, "answers": s.answers,
                                   "voice": s.voiceID ?? ""], forKey: Self.statusKey)
        if s.sampleRate != 24000 { player = PCMPlayer(sampleRate: s.sampleRate) }
        guard s.cartesia else { return }
        await prepareCues(gateway: gateway, voice: s.voiceID ?? "")
    }

    /// The fixed cue lines as PCM in memory: from the phone's cache, else from the gateway (cached there too).
    private func prepareCues(gateway: GatewayClient, voice: String) async {
        let dir = URL.cachesDirectory.appending(path: "lea-voice", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for line in VoiceCues.fixedLines where cuePCM[line] == nil {
            let file = dir.appending(path: Self.sha("\(voice)|\(line)") + ".pcm")
            if let data = try? Data(contentsOf: file), !data.isEmpty {
                cuePCM[line] = data
            } else if let data = try? await gateway.ttsPCM(line), !data.isEmpty {
                cuePCM[line] = data
                try? data.write(to: file, options: .atomic)
            }
        }
        log.info("voice cues ready: \(self.cuePCM.count, privacy: .public)/\(VoiceCues.fixedLines.count, privacy: .public)")
    }

    // MARK: - Scan cues

    /// The scan screen's cue changed. Important cues are said, paced by CueLimiter; a cue that has to wait is said
    /// later only if it is still on screen.
    func cueChanged(_ text: String?, diag: Diag? = nil) {
        currentCue = text
        if let diag { cueDiag = diag }
        guard !muted, let line = VoiceCues.line(for: text) else { return }
        consider(line)
    }

    private func consider(_ line: String, dynamic: Bool = false) {
        switch limiter.decide(line, now: now, busy: speaking) {
        case .speak:
            pendingCue?.cancel()
            sayCue(line)
        case .drop:
            break
        case .wait(let until):
            pendingCue?.cancel()
            let delay = max(0.05, until - now)
            pendingCue = Task { [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard !Task.isCancelled, let self, !self.muted,
                      dynamic || VoiceCues.line(for: self.currentCue) == line else { return }
                self.consider(line, dynamic: dynamic)
            }
        }
    }

    private func sayCue(_ line: String) {
        limiter.spoke(line, at: now)
        cueDiag?.log("voice_cue", ["line": line, "source": cuePCM[line] != nil || useCartesia ? "cartesia" : "device"])
        if let pcm = cuePCM[line] {
            let id = begin()
            lastSource = "cartesia"
            player.append(pcm, id: id)
            finishPlayer(id)
        } else if useCartesia {
            streamLine(line, keepCue: true)
        } else {
            speakOnDevice(line, id: begin())
        }
    }

    /// A one-off cue made from the backend's words (a lead: what the person suspects), paced like the others.
    func dynamicCue(_ line: String, diag: Diag?) {
        if let diag { cueDiag = diag }
        guard !muted, !line.isEmpty else { return }
        consider(line, dynamic: true)
    }

    // MARK: - Conversation streams

    /// A new answer: stops anything playing; PCM chunks and phone-voice sentences of this answer follow, in order.
    func beginStream() -> Int {
        pendingCue?.cancel()
        let id = begin()
        limiter.spoke(nil, at: now)
        return id
    }

    func appendStream(_ pcm: Data, stream id: Int, started: (@Sendable () -> Void)? = nil) {
        guard id == utterance, !muted else { return }
        lastSource = "cartesia"
        player.append(pcm, id: id, started: started)
    }

    /// A sentence the gateway sent without audio: the phone's voice says it (queued after the others).
    func queueOnDevice(_ text: String, stream id: Int) {
        guard id == utterance, !muted else { return }
        lastSource = "device"
        let u = AVSpeechUtterance(string: text)
        u.voice = Self.deviceVoice
        speaking = true
        synth.speak(u)
    }

    /// The answer is complete: returns when the last of it has been played.
    func endStream(_ id: Int) async {
        guard id == utterance else { return }
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            player.finish(id: id) { c.resume() }
        }
        while synth.isSpeaking, id == utterance { try? await Task.sleep(for: .milliseconds(100)) }
        if id == utterance { speaking = false }
    }

    func endStreamNow(_ id: Int) {
        if id == utterance { stop() }
    }

    // MARK: - Report and answers

    /// A report just arrived: say the verdict, the top problem and the first safety note, once per report revision.
    /// `logged` gets the time from this call to the first sound and the voice used.
    func reportLanded(_ report: ReportModel, key: String, force: Bool = false, caveat: String? = nil,
                      logged: @escaping @MainActor @Sendable (_ ms: Int, _ source: String) -> Void) {
        guard !muted else { return }
        var spoken = UserDefaults.standard.stringArray(forKey: Self.spokenKey) ?? []
        guard force || !spoken.contains(key) else { return }
        spoken.append(key)
        UserDefaults.standard.set(Array(spoken.suffix(200)), forKey: Self.spokenKey)
        say(ReportSpeech.lines(report, caveat: caveat), logged: logged)
    }

    /// Says `text` now (stopping anything else): streamed from the gateway, or the phone's own voice.
    func say(_ text: String, logged: (@MainActor @Sendable (_ ms: Int, _ source: String) -> Void)? = nil) {
        guard !muted, !text.isEmpty else { return }
        pendingCue?.cancel()
        let start = now
        let id = begin()
        limiter.spoke(nil, at: start)
        let report: @MainActor @Sendable (String) -> Void = { [weak self] source in
            guard let self else { return }
            self.lastSource = source
            logged?(Int(((self.now - start) * 1000).rounded()), source)
        }
        guard useCartesia, let gateway = app?.gateway else {
            speakOnDevice(text, id: id, started: { report("device") })
            return
        }
        let player = self.player
        streamTask = Task { [weak self] in
            let onStart: @Sendable () -> Void = { Task { @MainActor in report("cartesia") } }
            let played = await Self.stream(text, gateway: gateway, player: player, id: id, onStart: onStart)
            guard let self, id == self.utterance else { return }
            if played || Task.isCancelled {
                self.finishPlayer(id)
            } else {
                log.error("voice stream failed: the phone's own voice instead")
                self.speakOnDevice(text, id: id, started: { report("device") })
            }
        }
    }

    private func streamLine(_ text: String, keepCue: Bool) {
        guard let gateway = app?.gateway else { return }
        let id = begin()
        let player = self.player
        streamTask = Task { [weak self] in
            let played = await Self.stream(text, gateway: gateway, player: player, id: id, onStart: {})
            guard let self, id == self.utterance else { return }
            if played || Task.isCancelled { self.finishPlayer(id) } else { self.speakOnDevice(text, id: id) }
        }
    }

    /// Streams the gateway's PCM for `text` into the player, off the main actor. True when any audio played.
    @concurrent nonisolated private static func stream(_ text: String, gateway: GatewayClient, player: PCMPlayer, id: Int,
                                           onStart: @escaping @Sendable () -> Void) async -> Bool {
        var played = false
        do {
            let bytes = try await gateway.ttsStream(text)
            var buf = Data()
            buf.reserveCapacity(9600)
            for try await b in bytes {
                buf.append(b)
                // The first 50 ms goes out at once; then 200 ms at a time.
                if buf.count >= (played ? 9600 : 2400) {
                    player.append(buf, id: id, started: played ? nil : onStart)
                    played = true
                    buf.removeAll(keepingCapacity: true)
                }
                if Task.isCancelled { return played }
            }
            if !buf.isEmpty {
                player.append(buf, id: id, started: played ? nil : onStart)
                played = true
            }
        } catch {
            log.error("voice stream: \(error.localizedDescription, privacy: .public)")
        }
        return played
    }

    @discardableResult
    /// Plays a complete 16-bit mono WAV (the gateway's inline answer audio).
    func play(wav: Data, logged: (@MainActor @Sendable (_ ms: Int, _ source: String) -> Void)? = nil) -> Bool {
        guard !muted, let pcm = PCMPlayer.pcm(fromWAV: wav) else { return false }
        let start = now
        let id = begin()
        lastSource = "cartesia"
        player.append(pcm, id: id, started: { Task { @MainActor [weak self] in
            guard let self else { return }
            logged?(Int(((self.now - start) * 1000).rounded()), "cartesia")
        } })
        finishPlayer(id)
        return true
    }

    func stop() {
        utterance += 1
        pendingCue?.cancel()
        streamTask?.cancel()
        player.stop()
        if synth.isSpeaking { synth.stopSpeaking(at: .immediate) }
        speaking = false
    }

    // MARK: - Plumbing

    private func begin() -> Int {
        utterance += 1
        streamTask?.cancel()
        if synth.isSpeaking { synth.stopSpeaking(at: .immediate) }
        player.begin(utterance)
        speaking = true
        return utterance
    }

    private func finishPlayer(_ id: Int) {
        player.finish(id: id) { Task { @MainActor [weak self] in
            guard let self, id == self.utterance else { return }
            self.speaking = false
        } }
    }

    private func speakOnDevice(_ text: String, id: Int, started: (@MainActor @Sendable () -> Void)? = nil) {
        guard id == utterance else { return }
        lastSource = "device"
        player.stop()
        let u = AVSpeechUtterance(string: text)
        u.voice = Self.deviceVoice
        u.prefersAssistiveTechnologySettings = false
        deviceStarted = started
        speaking = true
        synth.speak(u)
    }

    /// The best installed English voice (premium, then enhanced, then the default).
    private static let deviceVoice: AVSpeechSynthesisVoice? = {
        let en = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("en-US") }
        return en.first { $0.quality == .premium } ?? en.first { $0.quality == .enhanced } ?? AVSpeechSynthesisVoice(language: "en-US")
    }()

    static func sha(_ s: String) -> String {
        SHA256.hash(data: Data(s.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

extension LeaVoice: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        Task { @MainActor in
            self.deviceStarted?()
            self.deviceStarted = nil
        }
    }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in if !self.synth.isSpeaking { self.speaking = false } }
    }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in if !self.synth.isSpeaking { self.speaking = false } }
    }
}

/// The app's audio session, changed off the main thread (activation can take tens of milliseconds and must never
/// stall the scan's render loop). Playback ducks other audio and never needs the microphone; recording switches
/// to play-and-record only while the button is held.
enum VoiceAudioSession {
    private static let queue = DispatchQueue(label: "lea.voice.session", qos: .userInitiated)

    /// Speaking only. With "Hey Lea" on, the session stays play-and-record (switching categories under a playing
    /// engine would cut Lea off), so this is the same category the wake listener uses.
    static func playback() {
        let wake = UserDefaults.standard.object(forKey: WakeListener.enabledKey) as? Bool ?? true
        queue.async {
            let s = AVAudioSession.sharedInstance()
            do {
                if wake {
                    try s.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .duckOthers])
                } else {
                    try s.setCategory(.playback, mode: .voicePrompt, options: [.duckOthers, .interruptSpokenAudioAndMixWithOthers])
                }
                try s.setActive(true)
            } catch { log.error("audio session (playback): \(error.localizedDescription, privacy: .public)") }
        }
    }

    static func record() async -> Bool {
        await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            queue.async {
                let s = AVAudioSession.sharedInstance()
                do {
                    try s.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .duckOthers])
                    try s.setActive(true)
                    c.resume(returning: true)
                } catch {
                    log.error("audio session (record): \(error.localizedDescription, privacy: .public)")
                    c.resume(returning: false)
                }
            }
        }
    }
}

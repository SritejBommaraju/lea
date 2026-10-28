@preconcurrency import AVFoundation
import AudioToolbox
import Foundation
@preconcurrency import Speech
import UIKit

/// "Hey Lea" while the app is open: Apple's speech recognizer, on the device only (nothing leaves the phone and
/// nothing is billed until the phrase is heard). On a match: a soft chime and a tap, then the conversation, with
/// any words said after the phrase as the first thing the person said. After Lea answers it listens for a
/// follow-up for a few seconds without the phrase (hands-free turns); the turn ends when the person stops talking.
///
/// It listens only in the foreground and never while the talk button records. While Lea speaks, Apple's recognizer is
/// ignored (she must not hear herself); with the mic's echo cancellation on, Ink-2 keeps listening during her
/// answer, and the person starting to talk (`turn.start`) stops her: barge-in by voice.
@MainActor
final class WakeListener: ObservableObject {
    static let shared = WakeListener()
    nonisolated static let enabledKey = "leaWakeWordEnabled"
    nonisolated static let continuousKey = "leaHandsFreeContinuous"

    enum Mode: Equatable { case off, wake, followUp }
    @Published private(set) var mode: Mode = .off
    /// Goes up on each "Hey Lea" when no conversation is on screen (the home screen opens one).
    @Published private(set) var wakes = 0
    /// Words said after the phrase, for the conversation that opens.
    @Published var pendingUtterance: String?

    var enabled: Bool {
        get { UserDefaults.standard.object(forKey: Self.enabledKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: Self.enabledKey); refresh() }
    }

    /// Hands-free that stays on: after each answer Lea keeps listening for the next turn, with no window and no
    /// "Hey Lea", until it is turned off (swipe up turns it on; the ear button toggles it). Remembered.
    @Published var continuous: Bool = WakeListener.savedContinuous() {
        didSet {
            UserDefaults.standard.set(continuous, forKey: Self.continuousKey)
            if continuous { listenForFollowUp() } else { endFollowUp() }
        }
    }

    /// The saved hands-free switch. Once, with tap to talk (the owner, 2026-10-25), it starts off: Home's Talk button
    /// used to turn it on without asking, which kept reopening the mic after every answer.
    private static func savedContinuous() -> Bool {
        let d = UserDefaults.standard, reset = "leaHandsFreeTapToTalkReset"
        if !d.bool(forKey: reset) {
            d.set(true, forKey: reset)
            d.set(false, forKey: continuousKey)
        }
        return d.bool(forKey: continuousKey)
    }

    private let mic = MicTap()
    private var foreground = true
    private var leaTalking = false
    private var holdToTalk = false
    private var capturing = false
    private var authorized: Bool?
    /// The microphone is on or starting (kept here, on the main actor: never wait on the audio queue).
    private var micOn = false
    /// After the phrase: the words so far and when they last changed (the turn ends after `endSilence`).
    private var heardAfterWake: String?
    private var lastChange = Date()
    private var silenceTimer: Task<Void, Never>?
    private var followUpTimer: Task<Void, Never>?
    private var observers: [NSObjectProtocol] = []
    /// When the current stretch of speech started (first words of a transcript), for the wake latency.
    private var speechStart: Date?
    private var lastLogged = ""
    private var wokeAt: Date?
    private var heartbeat: Task<Void, Never>?
    private var leaStoppedAt = Date.distantPast
    private var stream: CartesiaTurns?
    private var streamOpening: Task<Void, Never>?
    private var streamWatchdog: Task<Void, Never>?
    /// Ink-2 has heard words in this turn (it owns the turn from then on).
    private var streamHeard = false
    /// The mic runs with voice processing (echo cancellation): Lea's own voice is taken out of what it hears.
    private var echoCancelled = false
    /// Ink-2 is open during Lea's answer, for the person to cut in (barge-in).
    private var bargeListening = false
    /// How long Ink-2 may stay quiet after opening before Apple takes the turn back.
    static let streamGrace: TimeInterval = 3.5
    static let echoGuard: TimeInterval = 0.7
    static let endSilence: TimeInterval = 1.1
    static let followUpWindow: TimeInterval = 9

    /// `Documents/voice/diag.jsonl`: what the listener did (start/stop and why, matches, turns, latency). Words
    /// are logged only around a match attempt (a greeting or a name-like word), never whole conversations.
    static let diag: Diag = {
        let dir = URL.documentsDirectory.appending(path: "voice", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return Diag.session(dir)
    }()

    init() {
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in WakeListener.shared.foreground = true; WakeListener.shared.refresh() }
        })
        observers.append(nc.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in WakeListener.shared.foreground = false; WakeListener.shared.refresh() }
        })
    }

    // MARK: Gates

    /// While Lea speaks, what the mic hears is ignored (she must not hear herself); the mic itself stays on,
    /// because starting or stopping audio input reconfigures the output and would cut her off.
    func leaSpeaking(_ on: Bool) {
        guard on != leaTalking else { return }
        leaTalking = on
        if on {
            silenceTimer?.cancel()
            if heardAfterWake != nil { heardAfterWake = nil }
            if bargeInOn {
                // Echo cancellation keeps Lea out of the mic: Ink-2 stays open so the person can cut in.
                bargeListening = true
                if stream == nil { openStream(backfill: 0) }
            } else {
                closeStream()
            }
        } else {
            leaStoppedAt = Date()
            if micOn { mic.restart() }   // a fresh transcript: none of Lea's words carry over
        }
    }
    func suspend(_ on: Bool) { holdToTalk = on; refresh() }
    /// ScanView: Lea keeps listening during a capture, so the person can talk to her while scanning (the scan bar).
    /// `--no-wake-during-scan` pauses it instead, to measure the scan's frame time without the recognizer.
    func capture(_ on: Bool) {
        capturing = on && CommandLine.arguments.contains("--no-wake-during-scan")
        refresh()
    }

    /// After Lea answers: listen for the next turn without the phrase, for a few seconds (or until turned off, when
    /// hands-free is continuous).
    func listenForFollowUp() {
        guard enabled else { return }
        mode = .followUp
        followUpTimer?.cancel()
        if !continuous {   // continuous hands-free has no window
            followUpTimer = Task {
                try? await Task.sleep(for: .seconds(Self.followUpWindow))
                guard !Task.isCancelled, mode == .followUp, heardAfterWake == nil, !streamHeard else { return }
                closeStream()
                mode = .wake
            }
        }
        refresh()
        openStream(backfill: 0.3)
    }

    // MARK: Ink-2 (realtime, after Samik's PR #11)

    /// Where the realtime token comes from (set by LeaVoice.attach): the paired gateway, when there is one.
    var gateway: () -> GatewayClient? = { nil }
    /// Ink-2 on Cartesia listens to the person's turn when the gateway has voice; Apple's recognizer is the fallback
    /// and keeps hearing the wake word. `--no-realtime` turns it off (to compare).
    var realtimeOn: Bool { LeaVoice.shared.status?.cartesia == true && !CommandLine.arguments.contains("--no-realtime") }
    /// Barge-in by voice: only with echo cancellation on, only during an answer of the conversation on screen (not
    /// scan cues), and not with `--no-barge-in`.
    private var bargeInOn: Bool {
        echoCancelled && realtimeOn && micOn && LeaConversation.active?.answering == true
            && !CommandLine.arguments.contains("--no-barge-in")
    }

    /// Opens an Ink-2 turn, sending the last `backfill` seconds of audio first (the words said while it connects).
    private func openStream(backfill: TimeInterval) {
        guard stream == nil, streamOpening == nil, realtimeOn, micOn, let gateway = gateway() else { return }
        let opened = Date()
        streamOpening = Task { [weak self] in
            async let grant = RealtimeTokens.grant(gateway)
            async let allowed = gateway.realtimeStream(kind: "stt")
            let (g, ok) = await (grant, allowed)
            guard let self else { return }
            self.streamOpening = nil
            guard !Task.isCancelled, let g, ok, self.micOn, !self.leaTalking || self.bargeListening,
                  self.mode == .followUp || self.heardAfterWake != nil || self.bargeListening else {
                if g == nil || !ok { Self.diag.log("realtime_open", ["ok": false, "token": g != nil, "allowed": ok]) }
                if self.heardAfterWake != nil { self.armSilence() }   // Apple finishes the turn
                return
            }
            let turns = CartesiaTurns(grant: g) { [weak self] e in Task { @MainActor in self?.streamEvent(e) } }
            self.stream = turns
            self.streamHeard = false
            self.mic.pcm.open(backfill: backfill + Date().timeIntervalSince(opened)) { [weak turns] d in turns?.send(d) }
            Self.diag.log("realtime_open", ["ok": true, "setup_ms": Int(Date().timeIntervalSince(opened) * 1000)])
            // No words from Ink-2 in time: it is Apple's turn again (and the socket goes).
            self.streamWatchdog = Task { [weak self] in
                try? await Task.sleep(for: .seconds(Self.streamGrace))
                // Open for a barge-in: it stays while Lea talks, then the usual grace after she stops.
                while let s = self, s.leaTalking, s.bargeListening, !Task.isCancelled { try? await Task.sleep(for: .milliseconds(250)) }
                if self?.bargeListening == true { try? await Task.sleep(for: .seconds(Self.streamGrace)) }
                guard let self, !Task.isCancelled, !self.streamHeard else { return }
                Self.diag.log("realtime_quiet", ["mode": "\(self.mode)"])
                self.closeStream()
                if self.heardAfterWake != nil { self.armSilence() }
            }
        }
    }

    private func closeStream() {
        streamOpening?.cancel()
        streamOpening = nil
        streamWatchdog?.cancel()
        mic.pcm.close()
        stream?.cancel()
        stream = nil
        streamHeard = false
        bargeListening = false
    }

    private func streamEvent(_ e: CartesiaTurns.Event) {
        guard stream != nil else { return }
        switch e {
        case .start:
            streamHeard = true
            followUpTimer?.cancel()
            if leaTalking {   // the person cut in: Lea stops now, and this is their turn
                Self.diag.log("barge_in", ["mode": "\(mode)"])
                mode = .followUp
                LeaConversation.active?.personStarted()
            } else {
                LeaConversation.active?.turnStarting()   // the camera now, uploaded while they talk
            }
        case .update(let t):
            streamHeard = true
            silenceTimer?.cancel()
            followUpTimer?.cancel()
            lastChange = Date()
            let words = Self.dropWake(t)
            if !words.isEmpty { caption(words) }
        case .eagerEnd(let t):
            LeaConversation.active?.speculate(Self.dropWake(t))
        case .resume:
            LeaConversation.active?.cancelSpeculation()
        case .end(let t):
            let from = mode == .followUp ? "follow_up" : "wake"
            let words = Self.dropWake(t)
            closeStream()
            heardAfterWake = nil
            mode = continuous && LeaConversation.active != nil ? .followUp : .wake
            speechStart = nil
            mic.restart()
            Self.diag.log("wake_turn", ["from": from, "engine": "ink2", "words": words.split(separator: " ").count,
                                        "wake_to_turn_ms": wokeAt.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1])
            wokeAt = nil
            guard !words.isEmpty else {
                LeaConversation.active?.cancelSpeculation()
                // Only "Hey Lea" so far (a pause before the request, 2026-10-24): keep listening for it, no phrase
                // needed, like Apple's longer wait after the phrase alone.
                if from == "wake" { silenceTimer?.cancel(); listenForFollowUp() }
                return
            }
            if let convo = LeaConversation.active { convo.heard(words, engine: "ink2") } else { pendingUtterance = words }
        case .failed(let why):
            Self.diag.log("realtime_failed", ["why": why])
            LeaConversation.active?.cancelSpeculation()
            closeStream()
            if heardAfterWake != nil { armSilence() }   // Apple finishes the turn
        }
    }

    /// The transcript without a leading "Hey Lea" (kept as the person said it otherwise).
    static func dropWake(_ t: String) -> String {
        guard let after = afterWake(t) else { return t.trimmingCharacters(in: .whitespaces) }
        let words = t.split(separator: " ")
        for k in 0..<min(words.count, 5) where normalize(words[k...].joined(separator: " ")) == after {
            return words[k...].joined(separator: " ")
        }
        return after
    }

    /// Follow-up listening ends: hands-free turned off, or its conversation left the screen. The Ink-2 stream closes
    /// and a half-heard turn is dropped, so nothing more is sent without "Hey Lea".
    func endFollowUp() {
        guard mode == .followUp else { return }
        followUpTimer?.cancel()
        silenceTimer?.cancel()
        closeStream()
        heardAfterWake = nil
        speechStart = nil
        LeaConversation.active?.cancelSpeculation()
        mode = .wake
        mic.restart()
    }

    /// A typed turn took over: whatever was being heard is dropped (Apple's transcript starts over, Ink-2 closes), so
    /// it can't become a turn of its own.
    func dropPendingTurn() {
        silenceTimer?.cancel()
        guard heardAfterWake != nil || stream != nil || streamOpening != nil else { return }
        heardAfterWake = nil
        speechStart = nil
        closeStream()
        mic.restart()
    }

    func refresh() {
        let should = enabled && foreground && !holdToTalk && !capturing
        if should { Task { await start() } } else { stop() }
    }

    // MARK: Listening

    private func start() async {
        guard !micOn else { return }
        micOn = true   // set before any await: refresh() can run again meanwhile
        if authorized == nil {
            authorized = await Self.authorize()
            Self.diag.log("wake_auth", ["ok": authorized ?? false,
                                        "mic": MicPermission.state.rawValue,
                                        "speech": "\(SFSpeechRecognizer.authorizationStatus().rawValue)"])
        }
        guard authorized == true, enabled, foreground, !holdToTalk, !capturing else { micOn = false; return }
        if mode == .off { mode = .wake }
        let result = await mic.start { [weak self] text, final in
            Task { @MainActor in self?.heard(text, final: final) }
        }
        echoCancelled = result.echoCancelled
        Self.diag.log("wake_start", ["ok": result.ok, "engine": result.engine, "reason": result.reason,
                                     "echo_cancelled": result.echoCancelled])
        if !result.ok { mode = .off; micOn = false }
        if result.ok, heartbeat == nil {
            heartbeat = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(15))
                    guard let self, self.micOn else { continue }
                    let mic = self.mic
                    let (r, e, b, last) = await Task.detached { mic.takeCounts() }.value
                    Self.diag.log("wake_alive", ["mode": "\(self.mode)", "results": r, "errors": e, "buffers": b,
                                                 "last_error": last, "lea_talking": self.leaTalking])
                }
            }
        }
        // Gates may have closed while the engine was starting.
        if !(enabled && foreground && !holdToTalk && !capturing) { stop() }
    }

    private func stop() {
        silenceTimer?.cancel()
        if heardAfterWake != nil { finishTurn() }
        guard micOn else { return }
        Self.diag.log("wake_stop", ["enabled": enabled, "foreground": foreground, "hold": holdToTalk, "capture": capturing])
        closeStream()
        micOn = false
        mic.stop()
        if mode == .wake { mode = .off }
    }

    private func heard(_ text: String, final: Bool) {
        // "Stop" ends a walkthrough at once; while Lea speaks only with her name ("Lea, stop"), as the mic hears her too.
        let echo = leaTalking || Date().timeIntervalSince(leaStoppedAt) <= Self.echoGuard
        if let tour = WalkthroughPlayer.current, tour.heard(text, leaSpeaking: echo) {
            Self.diag.log("walkthrough_stop", ["lea_speaking": echo])
            dropTurn()
            return
        }
        // Not while Lea speaks, nor in the echo of her last words.
        guard !echo else { return }
        let words = Self.normalize(text)
        if speechStart == nil, !words.isEmpty { speechStart = Date() }
        if heardAfterWake == nil, mode == .wake, Self.looksLikeAnAttempt(words) {
            let tail = words.split(separator: " ").suffix(6).joined(separator: " ")
            if tail != lastLogged { lastLogged = tail; Self.diag.log("wake_heard", ["tail": tail, "match": Self.afterWake(words) != nil]) }
        }
        if mode == .followUp || heardAfterWake != nil {
            // Ink-2 owns the turn once it hears words; until then Apple keeps the captions going.
            if stream != nil, streamHeard { return }
            let after = heardAfterWake == nil ? words : Self.afterWake(words) ?? words
            if after != heardAfterWake { heardAfterWake = after; lastChange = Date(); caption(after) }
            followUpTimer?.cancel()
            armSilence()
            if final { finishTurn() }
            return
        }
        guard let after = Self.afterWake(words) else {
            if final { speechStart = nil }
            return
        }
        // "Hey Lea": chime, tap, and open the conversation; the turn continues until the person stops talking.
        AudioServicesPlaySystemSound(1113)
        UIImpactFeedbackGenerator(style: .soft).impactOccurred()
        wokeAt = Date()
        Self.diag.log("wake_match", ["speech_to_match_ms": speechStart.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1,
                                     "words_after": after.split(separator: " ").count,
                                     "screen": LeaConversation.active == nil ? "none" : (LeaConversation.active?.scanID == PreScan.shared.scanID ? "home_sheet" : "report")])
        heardAfterWake = after
        lastChange = Date()
        if LeaConversation.active == nil { pendingUtterance = nil; wakes += 1 }
        caption(after)
        armSilence(longer: after.isEmpty)
        openStream(backfill: 2.0)   // the words after "Hey Lea" go to Ink-2 (with the audio said so far)
    }

    /// What was heard was not a turn (it ended the walkthrough): forget it and listen for "Hey Lea" again.
    private func dropTurn() {
        silenceTimer?.cancel()
        followUpTimer?.cancel()
        closeStream()
        heardAfterWake = nil
        speechStart = nil
        wokeAt = nil
        mode = .wake
        mic.restart()
        LeaConversation.active?.turnDropped()
    }

    private func caption(_ text: String) {
        LeaConversation.active?.showLive(text)
    }

    private func armSilence(longer: Bool = false) {
        silenceTimer?.cancel()
        let wait = longer ? 4.0 : Self.endSilence
        silenceTimer = Task {
            try? await Task.sleep(for: .seconds(wait))
            guard !Task.isCancelled, Date().timeIntervalSince(lastChange) >= wait - 0.05 else { return }
            finishTurn()
        }
    }

    /// The person stopped talking: their words go to the conversation, and listening starts over.
    private func finishTurn() {
        // Ink-2 is connecting or listening: its turn.end decides (the watchdog hands back if it stays quiet).
        if stream != nil || streamOpening != nil {
            if streamHeard || stream == nil || Date().timeIntervalSince(stream!.openedAt) < Self.streamGrace { return }
        }
        silenceTimer?.cancel()
        let words = (heardAfterWake ?? "").trimmingCharacters(in: .whitespaces)
        let from = mode == .followUp ? "follow_up" : "wake"
        // A single stray word in the follow-up window is not a turn (someone nearby, a cough), except a plain
        // yes or no: that answers Lea's question ("Want to scan it?" "Yes.").
        let tooShort = from == "follow_up" && words.split(separator: " ").count < 2 && !Self.isShortAnswer(words)
        heardAfterWake = nil
        mode = continuous && LeaConversation.active != nil ? .followUp : .wake
        speechStart = nil
        mic.restart()
        Self.diag.log("wake_turn", ["from": from, "words": words.split(separator: " ").count,
                                    "wake_to_turn_ms": wokeAt.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1])
        wokeAt = nil
        // Only "Hey Lea", then a pause (Apple's path; the Ink-2 path does the same): keep listening for the request,
        // no phrase needed (the owner's phone, 2026-10-25: two empty turns after the phrase).
        if words.isEmpty, from == "wake" { listenForFollowUp(); return }
        guard !words.isEmpty, !tooShort else { return }
        if let convo = LeaConversation.active { convo.heard(words) } else { pendingUtterance = words }
    }

    // MARK: Matching

    /// One-word answers that are a turn on their own: a yes (LeaConversation.isYes, the same words that take an offer)
    /// or a no.
    static func isShortAnswer(_ words: String) -> Bool {
        let w = normalize(words)
        return !w.contains(" ") && (LeaConversation.isYes(w) || w == "no" || w == "nope")
    }

    /// Lower-case letters and spaces only.
    static func normalize(_ s: String) -> String {
        s.lowercased().map { $0.isLetter || $0 == "'" ? $0 : " " }.reduce(into: "") { $0.append($1) }
            .split(separator: " ").joined(separator: " ")
    }

    /// The words after "hey lea" (tolerant: hey/hi/hay/okay + lea/leah/lia/leia/leya/lee a), "" right after the
    /// phrase, nil when the phrase is not there.
    static func afterWake(_ words: String) -> String? {
        let w = " " + normalize(words) + " "
        for joined in [" hey lea ", " heylea ", " hilea ", " haylea ", " healy ", " heyleah "] {
            if let r = w.range(of: joined) { return String(w[r.upperBound...]).trimmingCharacters(in: .whitespaces) }
        }
        for name in names {
            for greet in greetings {
                if let r = w.range(of: greet + name) {
                    // "a lea" only at the start of what was said: "a" alone is too common mid-sentence.
                    if greet == " a", w[..<r.lowerBound].split(separator: " ").count > 0 { continue }
                    return String(w[r.upperBound...]).trimmingCharacters(in: .whitespaces)
                }
            }
        }
        return nil
    }

    /// How the recognizer writes the name (seen and likely): "Lea", "Leah", "Lia", "Leia", "Lee", "Lee-ah"…
    static let names = [" lea ", " leah ", " lia ", " leia ", " leya ", " lee ah ", " lee a ", " leigh ", " lee ",
                        " leo ", " liya ", " lia's ", " lea's ", " leah's "]
    static let greetings = [" hey", " hi", " hay", " okay", " ok", " hello", " a"]

    /// A greeting or a name-like word: worth logging what the recognizer wrote (to tune the matching).
    static func looksLikeAnAttempt(_ words: String) -> Bool {
        let w = " " + words + " "
        return [" hey ", " hi ", " hay ", " lea ", " leah ", " lia ", " lee ", " leia ", " leigh "].contains { w.contains($0) }
    }

    private static func authorize() async -> Bool {
        guard await VoiceRecorder.allowed() else { return false }
        return await SpeechPermission.request()
    }
}

/// The microphone feeding Apple's on-device recognizer. Engine work runs on its own queue (starting an audio
/// engine can take tens of milliseconds); results come back through `onText`.
final class MicTap: @unchecked Sendable {
    private let queue = DispatchQueue(label: "lea.voice.wake", qos: .userInitiated)
    private var engine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var onText: (@Sendable (String, Bool) -> Void)?
    private var running = false
    private var restartTimer: DispatchWorkItem?
    /// The same audio as 16 kHz s16le for Ink-2 (with ~2 s kept for the words said while a socket connects).
    let pcm = PCM16Feed()
    /// For the diag heartbeat: results and errors since the last one, the last error, audio buffers seen.
    private(set) var results = 0, errors = 0, buffers = 0
    private(set) var lastError = ""

    /// (results, errors, buffers, last error) since the last call, then zeroed.
    func takeCounts() -> (Int, Int, Int, String) {
        queue.sync {
            defer { results = 0; errors = 0; buffers = 0 }
            return (results, errors, buffers, lastError)
        }
    }

    struct StartResult: Sendable {
        var ok: Bool
        var engine: String
        var reason: String
        var echoCancelled = false
    }

    func start(onText: @escaping @Sendable (String, Bool) -> Void) async -> StartResult {
        await withCheckedContinuation { (c: CheckedContinuation<StartResult, Never>) in
            queue.async { [self] in
                guard !running else { c.resume(returning: StartResult(ok: true, engine: "sf", reason: "running")); return }
                guard let recognizer else {
                    c.resume(returning: StartResult(ok: false, engine: "sf", reason: "no en-US recognizer"))
                    return
                }
                guard recognizer.isAvailable, recognizer.supportsOnDeviceRecognition else {
                    log.error("wake word: no on-device recognizer")
                    c.resume(returning: StartResult(ok: false, engine: "sf", reason:
                        "available=\(recognizer.isAvailable) on_device=\(recognizer.supportsOnDeviceRecognition)"))
                    return
                }
                self.onText = onText
                let s = AVAudioSession.sharedInstance()
                do {
                    try s.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .duckOthers])
                    try s.setActive(true)
                } catch {
                    c.resume(returning: StartResult(ok: false, engine: "sf", reason: "session: \(error.localizedDescription)"))
                    return
                }
                // Voice processing (echo cancellation): Lea's voice is taken out of the mic, so the person can cut in
                // while she talks (other audio ducked as little as allowed). If the engine will not start with it,
                // it starts without (no barge-in by voice). `--no-echo-cancel` skips it.
                let wantEcho = !CommandLine.arguments.contains("--no-echo-cancel")
                var started: (AVAudioEngine, Bool, AVAudioFormat)?
                var failure = "no input format"
                for echo in (wantEcho ? [true, false] : [false]) {
                    let engine = AVAudioEngine()
                    let input = engine.inputNode
                    let on = echo && (try? input.setVoiceProcessingEnabled(true)) != nil && input.isVoiceProcessingEnabled
                    if echo, !on { continue }
                    if on, #available(iOS 17.0, *) {
                        input.voiceProcessingOtherAudioDuckingConfiguration =
                            AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
                    }
                    let format = input.outputFormat(forBus: 0)
                    guard format.sampleRate > 0 else { failure = "no input format"; continue }
                    input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buf, _ in
                        self?.request?.append(buf)
                        self?.pcm.feed(buf)
                        self?.buffers += 1
                    }
                    do {
                        try engine.start()
                        started = (engine, on, format)
                        break
                    } catch {
                        input.removeTap(onBus: 0)
                        failure = "engine: \(error.localizedDescription)"
                    }
                }
                guard let got = started else {
                    c.resume(returning: StartResult(ok: false, engine: "sf", reason: failure))
                    return
                }
                let (engine, echo, format) = got
                self.engine = engine
                running = true
                beginRecognition()
                c.resume(returning: StartResult(ok: true, engine: "sf",
                                                reason: "\(Int(format.sampleRate)) Hz \(format.channelCount) ch", echoCancelled: echo))
            }
        }
    }

    func stop() {
        queue.async { [self] in
            guard running else { return }
            running = false
            restartTimer?.cancel()
            task?.cancel()
            request?.endAudio()
            task = nil
            request = nil
            engine?.inputNode.removeTap(onBus: 0)
            engine?.stop()
            engine = nil
        }
    }

    /// A fresh transcript (after a turn, and every 50 s: one recognition request is limited to about a minute).
    func restart() {
        queue.async { [self] in
            guard running else { return }
            task?.cancel()
            request?.endAudio()
            beginRecognition()
        }
    }

    private func beginRecognition() {
        guard let recognizer else { return }
        let r = SFSpeechAudioBufferRecognitionRequest()
        r.requiresOnDeviceRecognition = true
        r.shouldReportPartialResults = true
        r.taskHint = .dictation
        r.addsPunctuation = false
        request = r
        let onText = self.onText
        task = recognizer.recognitionTask(with: r) { [weak self] result, error in
            if let result { onText?(result.bestTranscription.formattedString, result.isFinal) }
            self?.queue.async {
                if result != nil { self?.results += 1 }
                if let error { self?.errors += 1; self?.lastError = "\((error as NSError).domain) \((error as NSError).code)" }
            }
            if error != nil || result?.isFinal == true {
                self?.queue.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                    guard let self, self.running, self.request === r else { return }
                    self.beginRecognition()
                }
            }
        }
        restartTimer?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.restart() }
        restartTimer = work
        queue.asyncAfter(deadline: .now() + 50, execute: work)
    }
}

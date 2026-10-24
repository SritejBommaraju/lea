@preconcurrency import AVFoundation
import AudioToolbox
import Foundation
import Observation
@preconcurrency import Speech
import UIKit

/// "Hey Lea" while the app is open: Apple's speech recognizer, on the device only (nothing leaves the phone and
/// nothing is billed until the phrase is heard). On a match: a soft chime and a tap, then the conversation, with
/// any words said after the phrase as the first thing the person said. After Lea answers it listens for a
/// follow-up for a few seconds without the phrase (hands-free turns); the turn ends when the person stops talking.
///
/// It listens only in the foreground, never while Lea is speaking (she must not hear herself), never while hold to
/// talk records, and not during an active scan capture (ScanView pauses it).
@MainActor
@Observable
final class WakeListener {
    static let shared = WakeListener()
    nonisolated static let enabledKey = "leaWakeWordEnabled"
    nonisolated static let continuousKey = "leaHandsFreeContinuous"

    enum Mode: Equatable { case off, wake, followUp }
    private(set) var mode: Mode = .off
    /// Goes up on each "Hey Lea" when no conversation is on screen (the home screen opens one).
    private(set) var wakes = 0
    /// Words said after the phrase, for the conversation that opens.
    var pendingUtterance: String?

    var enabled: Bool {
        get { UserDefaults.standard.object(forKey: Self.enabledKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: Self.enabledKey); refresh() }
    }

    /// Hands-free that stays on: after each answer Lea keeps listening for the next turn, with no window and no
    /// "Hey Lea", until it is turned off (swipe up turns it on; the ear button toggles it). Remembered.
    var continuous: Bool = UserDefaults.standard.bool(forKey: WakeListener.continuousKey) {
        didSet {
            UserDefaults.standard.set(continuous, forKey: Self.continuousKey)
            if continuous { listenForFollowUp() } else if mode == .followUp, heardAfterWake == nil { mode = .wake }
        }
    }

    @ObservationIgnored private let mic = MicTap()
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private var leaTalking = false
    @ObservationIgnored private var holdToTalk = false
    @ObservationIgnored private var capturing = false
    @ObservationIgnored private var authorized: Bool?
    /// The microphone is on or starting (kept here, on the main actor: never wait on the audio queue).
    @ObservationIgnored private var micOn = false
    /// After the phrase: the words so far and when they last changed (the turn ends after `endSilence`).
    @ObservationIgnored private var heardAfterWake: String?
    @ObservationIgnored private var lastChange = Date()
    @ObservationIgnored private var silenceTimer: Task<Void, Never>?
    @ObservationIgnored private var followUpTimer: Task<Void, Never>?
    @ObservationIgnored private var observers: [NSObjectProtocol] = []
    /// When the current stretch of speech started (first words of a transcript), for the wake latency.
    @ObservationIgnored private var speechStart: Date?
    @ObservationIgnored private var lastLogged = ""
    @ObservationIgnored private var wokeAt: Date?
    @ObservationIgnored private var heartbeat: Task<Void, Never>?
    @ObservationIgnored private var leaStoppedAt = Date.distantPast
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
        if continuous { refresh(); return }
        followUpTimer = Task {
            try? await Task.sleep(for: .seconds(Self.followUpWindow))
            guard !Task.isCancelled, mode == .followUp, heardAfterWake == nil else { return }
            mode = .wake
        }
        refresh()
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
                                        "mic": "\(AVAudioApplication.shared.recordPermission.rawValue)",
                                        "speech": "\(SFSpeechRecognizer.authorizationStatus().rawValue)"])
        }
        guard authorized == true, enabled, foreground, !holdToTalk, !capturing else { micOn = false; return }
        if mode == .off { mode = .wake }
        let result = await mic.start { [weak self] text, final in
            Task { @MainActor in self?.heard(text, final: final) }
        }
        Self.diag.log("wake_start", ["ok": result.ok, "engine": result.engine, "reason": result.reason])
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
        micOn = false
        mic.stop()
        if mode == .wake { mode = .off }
    }

    private func heard(_ text: String, final: Bool) {
        // Not while Lea speaks, nor in the echo of her last words.
        guard !leaTalking, Date().timeIntervalSince(leaStoppedAt) > Self.echoGuard else { return }
        let words = Self.normalize(text)
        if speechStart == nil, !words.isEmpty { speechStart = Date() }
        if heardAfterWake == nil, mode == .wake, Self.looksLikeAnAttempt(words) {
            let tail = words.split(separator: " ").suffix(6).joined(separator: " ")
            if tail != lastLogged { lastLogged = tail; Self.diag.log("wake_heard", ["tail": tail, "match": Self.afterWake(words) != nil]) }
        }
        if mode == .followUp || heardAfterWake != nil {
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
        silenceTimer?.cancel()
        let words = (heardAfterWake ?? "").trimmingCharacters(in: .whitespaces)
        let from = mode == .followUp ? "follow_up" : "wake"
        // A single stray word in the follow-up window is not a turn (someone nearby, a cough), except a plain
        // yes or no: that answers Lea's question ("Want to scan it?" "Yes.").
        let tooShort = from == "follow_up" && words.split(separator: " ").count < 2 && !Self.isShortAnswer(words)
        heardAfterWake = nil
        mode = continuous ? .followUp : .wake
        speechStart = nil
        mic.restart()
        Self.diag.log("wake_turn", ["from": from, "words": words.split(separator: " ").count,
                                    "wake_to_turn_ms": wokeAt.map { Int(Date().timeIntervalSince($0) * 1000) } ?? -1])
        wokeAt = nil
        guard !words.isEmpty, !tooShort else { return }
        if let convo = LeaConversation.active { convo.heard(words) } else { pendingUtterance = words }
    }

    // MARK: Matching

    /// One-word answers that are a turn on their own.
    static func isShortAnswer(_ words: String) -> Bool {
        ["yes", "yeah", "yep", "yup", "no", "nope", "sure", "okay", "ok", "alright", "please"].contains(normalize(words))
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

    struct StartResult: Sendable { var ok: Bool; var engine: String; var reason: String }

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
                let engine = AVAudioEngine()
                let input = engine.inputNode
                let format = input.outputFormat(forBus: 0)
                guard format.sampleRate > 0 else {
                    c.resume(returning: StartResult(ok: false, engine: "sf", reason: "no input format"))
                    return
                }
                input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buf, _ in
                    self?.request?.append(buf)
                    self?.buffers += 1
                }
                do { try engine.start() } catch {
                    input.removeTap(onBus: 0)
                    c.resume(returning: StartResult(ok: false, engine: "sf", reason: "engine: \(error.localizedDescription)"))
                    return
                }
                self.engine = engine
                running = true
                beginRecognition()
                c.resume(returning: StartResult(ok: true, engine: "sf", reason: "\(Int(format.sampleRate)) Hz"))
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

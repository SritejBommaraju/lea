import AVFoundation
import Combine
import Foundation

/// A licensed, device-tested "Hey Leah" detector plugs in here (for example a Porcupine keyword model). None is
/// bundled: transcribing everything is not a wake detector. Hands-free mode is the keyless alternative while the app
/// is open: on-device recognition, a conversation that listens all the time, as in VEL.
protocol WakeWordDetector: AnyObject {
    var name: String { get }
    /// Called on the audio thread with 16 kHz mono Int16 samples; returns true when the phrase is heard.
    nonisolated func process(_ samples: [Int16]) -> Bool
}

/// The single owner of the microphone and speaker. Conversation follows VEL's turn-taking: pressing (or, hands-free,
/// speaking) quiets Lea at once; Lea's own echo and backchannels are ignored; a quick fitting reaction plays while
/// the answer is fetched, thinking sounds hold a long wait, replies go sentence by sentence, and "go on" after an
/// interruption resumes at the clause Lea broke off. Only final transcripts reach the bridge.
@MainActor
final class VoiceCoordinator: ObservableObject {
    enum State: Equatable { case idle, listening, thinking, speaking }
    /// One back-and-forth: what the person said (or typed) and Lea's answer once it arrives.
    struct Exchange: Identifiable, Equatable {
        let id: String
        let said: String
        var reply: String?
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var partial = ""
    @Published private(set) var lastReply: String?
    /// This conversation so far; the gateway keeps the same history under `conversationID`.
    @Published private(set) var exchanges: [Exchange] = []
    private(set) var conversationID = VoiceCoordinator.newID("vc")
    @Published private(set) var handsFree = false
    /// Microphone loudness, 0...1, about ten times a second while the mic is open (the scan bar's meter).
    @Published private(set) var level: Float = 0
    @Published private(set) var wakeOn = false
    @Published private(set) var listenerName = "Apple on-device"
    @Published var message: String?

    var wakeDetector: WakeWordDetector?
    var wakeAvailable: Bool { wakeDetector != nil }

    private var engine = AVAudioEngine()
    private var player = AVAudioPlayerNode()
    private var observers: [NSObjectProtocol] = []
    private let pipe = AudioPipe()
    private var input: SpeechInput?
    private var output: SpeechOutput = SystemVoiceOutput()
    /// How spoken turns and speech tokens reach the gateway; set by the screen that shows the voice bar.
    var link: VoiceLink?
    /// What the gateway asked the phone to do (start a scan, open a report).
    var onActions: (([VoiceAction]) -> Void)?
    /// Read at each turn: what the open screen shows (a scan in progress, a report). Nil on the home screen.
    var scene: (() -> VoiceScene)?
    private var cartesiaUnavailable = false

    private var recentReactions: [String] = []
    private var recentlySpoken: [String] = []        // the last few lines, so a late echo of any of them is ignored
    private var turn = 0                             // bumped by each new turn and by interruptions: stale turns stop
    private var holding = false                      // push-to-talk button is down
    private var grant: (value: CartesiaGrant, until: Date)?
    private var queue: [String] = []                 // sentences still to say
    private var current = ""
    private var interrupted: (sentence: String, heard: Double, rest: [String])?
    private var resumes = 0
    private var speaking: Task<Void, Never>?
    private var turnMode = "ptt"
    private var recognizerErrors = 0                 // in a row; hands-free retries a couple of times before stopping
    private var listenVersion = 0                    // bumped by each listen and each stop: a stale listen gives up
    private var playbackEnded = Date.distantPast     // when Lea last stopped making sound
    private var heardOverPlayback = false            // this turn's words started while Lea was audible

    /// Starts watching for audio interruptions and route changes (once).
    func attach() {
        guard observers.isEmpty else { return }
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        observers = [
            center.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] note in
                let began = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
                MainActor.assumeIsolated { self?.audioInterrupted(began: began) }
            },
            center.addObserver(forName: AVAudioSession.routeChangeNotification, object: session, queue: .main) { [weak self] note in
                let reason = AVAudioSession.RouteChangeReason(rawValue: note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt ?? 0)
                // A different microphone or speaker can change the input format the tap was built for.
                guard reason == .newDeviceAvailable || reason == .oldDeviceUnavailable else { return }
                MainActor.assumeIsolated { self?.restartAudio(rebuild: false) }
            },
            center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: session, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.restartAudio(rebuild: true) }
            },
        ]
    }

    /// A call, Siri or an alarm took the audio: stop cleanly; when it ends, pick up listening as it was.
    private func audioInterrupted(began: Bool) {
        if began {
            turn += 1
            listenVersion += 1
            _ = output.stop()
            speaking?.cancel()
            input?.cancel()
            input = nil
            pipe.input = nil
            state = .idle
        } else {
            restartAudio(rebuild: false)
        }
    }

    /// Rebuilds the mic tap for the current route (or the whole engine after a media-services reset), then resumes
    /// hands-free or "Hey Leah" if either was on. A turn in progress is dropped rather than resumed half-heard.
    private func restartAudio(rebuild: Bool) {
        listenVersion += 1
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        input?.cancel()
        input = nil
        pipe.input = nil
        if rebuild {
            engine = AVAudioEngine()
            player = AVAudioPlayerNode()
            output = SystemVoiceOutput()
            grant = nil
        }
        if state != .speaking { state = .idle }
        if handsFree { Task { await listen(open: true) } }
        else if wakeOn { do { try startEngine(); pipe.wake = wakeDetector } catch { wakeOn = false; message = error.localizedDescription } }
    }

    /// One whiteboard line: waits for Lea to finish speaking, then says it (stops early if interrupted).
    func narrate(_ line: String) async {
        while state == .speaking || state == .thinking { try? await Task.sleep(for: .milliseconds(100)) }
        let mine = turn
        await say(line)
        if mine == turn { state = handsFree ? .listening : .idle }
    }

    // MARK: Entry points

    /// Hold to talk: quiets Lea at once and listens until release.
    func pushToTalkBegan() {
        if state == .speaking || state == .thinking { interrupt() }
        holding = true
        turnMode = "ptt"
        Task { await listen(open: false) }
    }

    /// A release before the recognizer is ready is remembered: `listen` finishes the turn once it starts.
    func pushToTalkEnded() {
        holding = false
        if turnMode == "ptt", let input { finishSoon(input) }
    }

    /// VEL streams 300 ms past the release so the last word isn't clipped.
    private func finishSoon(_ target: SpeechInput) {
        Task {
            try? await Task.sleep(for: .milliseconds(300))
            if target === input { target.finish() }
        }
    }

    /// "Hey Leah": the mic engine stays running for the detector while the app is open; each detection opens one turn.
    func setWake(_ on: Bool) {
        guard wakeDetector != nil else { message = "No “Hey Leah” model is installed yet. Use push-to-talk or hands-free."; return }
        wakeOn = on
        if on {
            do { try startEngine(); pipe.wake = wakeDetector } catch { wakeOn = false; message = error.localizedDescription }
        } else {
            pipe.wake = nil
            if state == .idle { stopEngine() }
        }
    }

    /// A fresh conversation, as when Siri opens the app: Lea forgets nothing on the phone until this is called.
    func newConversation() {
        conversationID = Self.newID("vc")
        exchanges = []
        lastReply = nil
    }

    /// Opened by Siri ("Hey Siri, hey lea"): say a short greeting, then keep the conversation going hands-free, so
    /// each answer is followed by listening again and the person can keep explaining.
    func greetAndListen(_ greeting: String = "Hey, what's up?") {
        guard state == .idle || state == .listening else { return }
        handsFree = true
        turnMode = "wake"
        Task {
            await say(greeting)
            if state == .speaking { state = .idle }
            await listen(open: true)
        }
    }

    /// A typed turn, for detail that is easier to type than say; it joins the same conversation.
    func send(typed: String) {
        if state == .speaking || state == .thinking {
            interrupt()
            if !handsFree { state = .idle }   // typing opens no microphone
        }
        turnMode = "text"
        Task { await take(typed, fromMic: false) }
    }

    /// Hands-free: the mic stays open for the conversation and speech itself takes the turn.
    func setHandsFree(_ on: Bool) {
        handsFree = on
        if on {
            turnMode = "wake"
            Task { await listen(open: true) }
        } else {
            listenVersion += 1   // a listen still waiting for its token or permission must not reopen the mic
            input?.cancel()
            input = nil
            pipe.input = nil
            if state != .speaking { stopEngine(); state = .idle }
        }
    }

    /// A short aside (a camera tip) that never interrupts Lea or the person.
    func sayAside(_ text: String) {
        guard !text.isEmpty, state == .idle || (handsFree && state == .listening && partial.isEmpty) else { return }
        speakAll([text])
    }

    // MARK: Listening

    private func listen(open: Bool) async {
        listenVersion += 1
        let version = listenVersion
        do {
            try startEngine()
            input?.cancel()
            input = nil
            pipe.input = nil
            let next = try await makeInput()
            // Stopped (hands-free off, an interruption) or replaced by a newer listen while the token or permission
            // was pending: give up rather than show Listening on a microphone that isn't running.
            guard version == listenVersion else { return }
            // Only the current recognizer's events count: a replaced one's late words are not a turn.
            next.onEvent = { [weak self, weak next] event in
                guard let self, let next, self.input === next else { return }
                self.handle(event)
            }
            try next.begin(open: open)
            heardOverPlayback = false
            input = next
            pipe.input = next
            partial = ""
            if state != .speaking { state = .listening }
            if !open, !holding { finishSoon(next) }
        } catch {
            guard version == listenVersion else { return }
            message = error.localizedDescription
            if state != .speaking { state = .idle }
        }
    }

    private func makeInput() async throws -> SpeechInput {
        if !cartesiaUnavailable, let grant = await cartesiaGrant() {
            listenerName = "Cartesia Ink 2"
            if state == .idle || state == .listening {
                output = CartesiaVoiceOutput(grant: grant, engine: engine, player: player) { [weak self] why in
                    self?.cartesiaVoiceFailed(why)
                }
            }
            return CartesiaSpeechInput(grant: grant)
        }
        guard await AppleSpeechInput.authorize() else {
            throw NSError(domain: "Lea", code: 2, userInfo: [NSLocalizedDescriptionKey: "Allow speech recognition in Settings so Lea can hear you."])
        }
        let apple = AppleSpeechInput()
        listenerName = apple.name
        if !(output is SystemVoiceOutput), state == .idle || state == .listening { output = SystemVoiceOutput() }
        return apple
    }

    /// Cartesia's voice failed mid-answer: say so, finish this answer in the iPhone voice, and fetch a fresh token for
    /// the next turn (a refused token would fail the same way again).
    private func cartesiaVoiceFailed(_ why: String) {
        message = "Lea's voice dropped (\(why)), so the iPhone voice is finishing the answer."
        grant = nil
        output = SystemVoiceOutput()
    }

    /// One Cartesia token serves every turn until shortly before it expires, so a turn doesn't wait for a fetch.
    private func cartesiaGrant() async -> CartesiaGrant? {
        if let grant, grant.until > Date() { return grant.value }
        guard let link else { return nil }
        guard let fresh = try? await link.token() else {
            cartesiaUnavailable = true   // not set up on the gateway: on-device speech for the rest of this run
            return nil
        }
        grant = (fresh, Date().addingTimeInterval(Double(max(fresh.expiresInSeconds - 30, 0))))
        return fresh
    }

    private func handle(_ event: SpeechEvent) {
        switch event {
        case .speechStart:
            if leaAudible { heardOverPlayback = true }
        case .partial(let text):
            partial = text
            if leaAudible { heardOverPlayback = true }
            // Barge-in, as VEL does hands-free, but only on words that aren't Lea's own voice or a backchannel:
            // the system voice is not echo-cancelled, and a cough must not cut Lea off.
            if handsFree, state == .speaking, text.split(separator: " ").count >= 2 || TurnTaking.isStop(text),
               !TurnTaking.isEcho(heard: text, spoken: recentlySpoken.joined(separator: " ")), !TurnTaking.isBackchannel(text) {
                interrupt()
            }
        case .transcript(let text):
            partial = ""
            recognizerErrors = 0
            Task { await take(text) }
        case .noSpeech:
            partial = ""
            afterTurn()
        case .error(let text):
            partial = ""
            // One hiccup must not end a conversation in the middle of a scan: hands-free listens again after a
            // moment. A recognizer that keeps failing would loop, so after three in a row hands-free stops and says why.
            recognizerErrors += 1
            if handsFree && recognizerErrors < 3 {
                pipe.input = nil
                input = nil
                Task {
                    try? await Task.sleep(for: .seconds(1))
                    if handsFree, input == nil { await listen(open: true) }
                }
            } else {
                message = text
                recognizerErrors = 0
                if handsFree { setHandsFree(false) } else { afterTurn() }
            }
        }
    }

    private func afterTurn() {
        pipe.input = nil
        input = nil
        if handsFree { Task { await listen(open: true) } }
        else if state != .speaking && state != .thinking { stopEngine(); state = .idle }
    }

    // MARK: A turn

    /// Lea is making sound now, or stopped a moment ago (the room still carries it).
    private var leaAudible: Bool { state == .speaking || Date().timeIntervalSince(playbackEnded) < 1.5 }

    private func take(_ heard: String, fromMic: Bool = true) async {
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        // Echo is only possible for microphone words heard over Lea's own voice; typed words are never echo, and a
        // person repeating Lea's words after she stopped ("scan it") means them.
        let maybeEcho = heardOverPlayback || leaAudible
        heardOverPlayback = false
        if fromMic { afterTurn() }
        guard !text.isEmpty, !TurnTaking.dropAsEcho(text, spoken: recentlySpoken.joined(separator: " "), fromMic: fromMic,
                                                    overPlayback: maybeEcho) else { return }
        if let stopped = interrupted, TurnTaking.isBack(text, offered: true) || TurnTaking.isBackchannel(text) {
            interrupted = nil
            let line = TurnTaking.resumeLine(stopped.sentence, heard: stopped.heard, n: resumes)
            resumes += 1
            return speakAll([line.text] + stopped.rest)
        }
        guard TurnTaking.takesTurn(text, whileLeaTalks: state == .speaking || state == .thinking) else { return }   // "go on" still resumes
        interrupted = nil
        turn += 1
        let mine = turn
        state = .thinking
        partial = text
        let id = Self.newID("vt")
        exchanges.append(Exchange(id: id, said: text))

        // Ask the bridge now; react at once; hold the turn with a thinking sound if the answer is slow.
        let scene = scene?()
        let request = VoiceTurnRequest(id: id, mode: turnMode, transcript: text, conversationID: conversationID,
                                       sessionID: scene?.sessionID, live: scene?.live ?? [:])
        guard let link else { message = "Pair with the gateway in Settings to talk to Lea."; state = handsFree ? .listening : .idle; return }
        let reply = Task { () -> VoiceReply? in
            do { return try await link.turn(request) } catch {
                await MainActor.run { self.message = "Lea couldn't reach the gateway: \(error.localizedDescription)" }
                return nil
            }
        }
        let reaction = TurnTaking.reaction(for: text, recent: recentReactions)
        if let reaction {
            recentReactions.append(reaction)
            await say(reaction)
            guard mine == turn else { return }
        }
        var fillers: [String] = []
        var answer: VoiceReply?
        var settled = false
        for n in 0..<3 {
            if let r = await firstWithin(reply, seconds: TurnTaking.fillQuiet(n)) { answer = r ?? nil; settled = true; break }
            if let f = TurnTaking.filler(after: fillers, n: n) { fillers.append(f); await say(f) }
            guard mine == turn else { return }
        }
        if !settled { answer = (await reply.value) ?? nil }
        // Interrupted while waiting: the newer turn speaks; this reply is kept in the session, not voiced.
        guard mine == turn else { return }
        guard let answer else { state = handsFree ? .listening : .idle; return }
        lastReply = answer.reply
        if let i = exchanges.firstIndex(where: { $0.id == id }) { exchanges[i].reply = answer.reply }
        onActions?(answer.actions)
        let reacted = reaction != nil || !fillers.isEmpty
        speakAll(TurnTaking.sentences(reacted ? TurnTaking.stripLead(answer.reply) : answer.reply))
    }

    private static func newID(_ prefix: String) -> String {
        "\(prefix)_\(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased())"
    }

    // MARK: Speaking

    private func remember(_ line: String) {
        recentlySpoken = Array((recentlySpoken + [line]).suffix(3))
    }

    private func say(_ line: String) async {
        remember(line)
        state = .speaking
        await output.speak(line)
        playbackEnded = Date()
    }

    private func speakAll(_ lines: [String]) {
        speaking?.cancel()
        queue = lines
        speaking = Task {
            state = .speaking
            while !queue.isEmpty, !Task.isCancelled {
                current = queue.removeFirst()
                remember(current)
                await output.speak(current)
                playbackEnded = Date()
            }
            if !Task.isCancelled {
                current = ""
                state = handsFree ? .listening : .idle
                if !handsFree { stopEngine() }
            }
        }
    }

    private func interrupt() {
        turn += 1
        let heard = output.stop()
        playbackEnded = Date()
        speaking?.cancel()
        if !current.isEmpty { interrupted = (current, heard, queue) }
        queue = []
        current = ""
        state = .listening
    }

    // MARK: Audio engine

    private func startEngine() throws {
        guard !engine.isRunning else { return }
        let session = AVAudioSession.sharedInstance()
        // voiceChat turns on the iPhone's echo cancellation, so Lea's voice is not heard back as speech.
        try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try session.setActive(true)
        if player.engine == nil { engine.attach(player) }
        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        pipe.prepare(from: format)
        pipe.wake = wakeOn ? wakeDetector : nil
        pipe.onLevel = { [weak self] value in Task { @MainActor in self?.level = value } }
        pipe.onWake = { [weak self] in
            Task { @MainActor in
                guard let self, self.state == .idle else { return }
                self.turnMode = "wake"
                await self.listen(open: true)
            }
        }
        let pipe = pipe
        inputNode.installTap(onBus: 0, bufferSize: 1600, format: format) { buffer, _ in pipe.push(buffer) }
        engine.prepare()
        try engine.start()
    }

    private func stopEngine() {
        guard !wakeOn, engine.isRunning else { return }   // "Hey Leah" keeps the engine listening while it is on
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        level = 0
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

/// Audio-thread side: hands each mic buffer to the active recognizer, plus a 16 kHz mono Int16 copy in 100 ms
/// chunks for recognizers and wake detectors that want that format.
nonisolated final class AudioPipe: @unchecked Sendable {
    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
    private var pending: [Int16] = []
    private var _input: SpeechInput?
    private var _wake: WakeWordDetector?
    var onWake: (() -> Void)?
    var onLevel: ((Float) -> Void)?
    private var levelFrames: AVAudioFrameCount = 0

    var input: SpeechInput? { get { lock.withLock { _input } } set { lock.withLock { _input = newValue } } }
    var wake: WakeWordDetector? { get { lock.withLock { _wake } } set { lock.withLock { _wake = newValue } } }

    func prepare(from format: AVAudioFormat) { converter = AVAudioConverter(from: format, to: target) }

    /// RMS of the first channel, mapped from -50...0 dB to 0...1, at most every 100 ms.
    private func reportLevel(_ buffer: AVAudioPCMBuffer) {
        levelFrames += buffer.frameLength
        guard levelFrames >= AVAudioFrameCount(buffer.format.sampleRate / 10), let onLevel,
              let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        levelFrames = 0
        var sum: Float = 0
        for i in 0..<Int(buffer.frameLength) { sum += samples[i] * samples[i] }
        let db = 10 * log10(max(sum / Float(buffer.frameLength), 1e-10))
        onLevel(min(1, max(0, (db + 50) / 50)))
    }

    func push(_ buffer: AVAudioPCMBuffer) {
        let input = self.input
        input?.feed(native: buffer)
        reportLevel(buffer)
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16_000 / buffer.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, let data = out.int16ChannelData else { return }
        pending.append(contentsOf: UnsafeBufferPointer(start: data[0], count: Int(out.frameLength)))
        while pending.count >= 1600 {
            let chunk = Array(pending.prefix(1600))
            pending.removeFirst(1600)
            if input == nil, let wake, wake.process(chunk) { onWake?() }
            input?.feed(pcm16k: chunk.withUnsafeBufferPointer { Data(buffer: $0) })
        }
    }
}

/// `.some(value)` if the task finishes within `seconds`, else nil (the task keeps running). Returns as soon as either
/// side wins: a task group would wait for its losing child, and a child awaiting `task.value` can't be cancelled, so a
/// slow reply would hold back the thinking sounds.
func firstWithin<T: Sendable>(_ task: Task<T, Never>, seconds: Double) async -> T?? {
    await withCheckedContinuation { (continuation: CheckedContinuation<T??, Never>) in
        let once = ResumeOnce(continuation)
        Task { once.resume(.some(await task.value)) }
        Task { try? await Task.sleep(for: .seconds(seconds)); once.resume(nil) }
    }
}

/// Resumes a continuation with whichever value arrives first; later values are dropped.
nonisolated final class ResumeOnce<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Never>?

    init(_ continuation: CheckedContinuation<T, Never>) { self.continuation = continuation }

    func resume(_ value: T) {
        let c: CheckedContinuation<T, Never>? = lock.withLock { defer { continuation = nil }; return continuation }
        c?.resume(returning: value)
    }
}

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

    @Published private(set) var state: State = .idle
    @Published private(set) var partial = ""
    @Published private(set) var lastReply: String?
    @Published private(set) var handsFree = false
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
    private weak var sync: SessionSync?
    private weak var actions: LeaActions?

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

    func attach(_ sync: SessionSync, actions: LeaActions) {
        self.sync = sync
        self.actions = actions
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

    /// Hands-free: the mic stays open for the conversation and speech itself takes the turn.
    func setHandsFree(_ on: Bool) {
        handsFree = on
        if on {
            turnMode = "wake"
            Task { await listen(open: true) }
        } else {
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
        do {
            try startEngine()
            input?.cancel()
            let next = try await makeInput()
            next.onEvent = { [weak self] event in self?.handle(event) }
            try next.begin(open: open)
            input = next
            pipe.input = next
            partial = ""
            if state != .speaking { state = .listening }
            if !open, !holding { finishSoon(next) }
        } catch {
            message = error.localizedDescription
            if state != .speaking { state = .idle }
        }
    }

    private func makeInput() async throws -> SpeechInput {
        if sync?.health?.voice.cartesiaConfigured == true, let grant = await cartesiaGrant() {
            listenerName = "Cartesia Ink 2"
            if state == .idle || state == .listening { output = CartesiaVoiceOutput(grant: grant, engine: engine, player: player) }
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

    /// One Cartesia token serves every turn until shortly before it expires, so a turn doesn't wait for a fetch.
    private func cartesiaGrant() async -> CartesiaGrant? {
        if let grant, grant.until > Date() { return grant.value }
        guard let fresh = try? await sync?.client?.cartesiaGrant() else { return nil }
        grant = (fresh, Date().addingTimeInterval(Double(max(fresh.expiresInSeconds - 30, 0))))
        return fresh
    }

    private func handle(_ event: SpeechEvent) {
        switch event {
        case .speechStart:
            break
        case .partial(let text):
            partial = text
            // Barge-in, as VEL does hands-free, but only on words that aren't Lea's own voice or a backchannel:
            // the system voice is not echo-cancelled, and a cough must not cut Lea off.
            if handsFree, state == .speaking, text.split(separator: " ").count >= 2 || TurnTaking.isStop(text),
               !TurnTaking.isEcho(heard: text, spoken: recentlySpoken.joined(separator: " ")), !TurnTaking.isBackchannel(text) {
                interrupt()
            }
        case .transcript(let text):
            partial = ""
            Task { await take(text) }
        case .noSpeech:
            partial = ""
            afterTurn()
        case .error(let text):
            partial = ""
            message = text
            // A recognizer that keeps failing would otherwise restart in a tight loop: hands-free stops instead.
            if handsFree { setHandsFree(false) } else { afterTurn() }
        }
    }

    private func afterTurn() {
        pipe.input = nil
        input = nil
        if handsFree { Task { await listen(open: true) } }
        else if state != .speaking && state != .thinking { stopEngine(); state = .idle }
    }

    // MARK: A turn

    private func take(_ heard: String) async {
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        afterTurn()
        guard !text.isEmpty, !TurnTaking.isEcho(heard: text, spoken: recentlySpoken.joined(separator: " ")) else { return }
        if let stopped = interrupted, TurnTaking.isBack(text, offered: true) || TurnTaking.isBackchannel(text) {
            interrupted = nil
            let line = TurnTaking.resumeLine(stopped.sentence, heard: stopped.heard, n: resumes)
            resumes += 1
            return speakAll([line.text] + stopped.rest)
        }
        if TurnTaking.isBackchannel(text) || TurnTaking.isStop(text) { return }   // stay quiet; "go on" still resumes
        interrupted = nil
        turn += 1
        let mine = turn
        state = .thinking
        partial = text

        // Ask the bridge now; react at once; hold the turn with a thinking sound if the answer is slow.
        let id = "vt_\(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased())"
        let mode = turnMode
        let sync = sync
        let frame = await actions?.frameProvider?()
        let focus = actions?.focusFindingId
        let reply = Task { await sync?.perform { client, remote in try await client.recordVoiceTurn(sessionId: remote, id: id, mode: mode, transcript: text, frame: frame, focus: focus) } }
        let reaction = TurnTaking.reaction(for: text, recent: recentReactions)
        if let reaction {
            recentReactions.append(reaction)
            await say(reaction)
            guard mine == turn else { return }
        }
        var fillers: [String] = []
        var answer: VoiceTurnResult?
        var settled = false
        for n in 0..<3 {
            if let r = await first(reply, within: TurnTaking.fillQuiet(n)) { answer = r ?? nil; settled = true; break }
            if let f = TurnTaking.filler(after: fillers, n: n) { fillers.append(f); await say(f) }
            guard mine == turn else { return }
        }
        if !settled { answer = (await reply.value) ?? nil }
        // Interrupted while waiting: the newer turn speaks; this reply is kept in the session, not voiced.
        guard mine == turn else { return }
        guard let answer else { state = handsFree ? .listening : .idle; return }
        lastReply = answer.turn.replyText
        actions?.perform(answer.actions)
        let reacted = reaction != nil || !fillers.isEmpty
        speakAll(TurnTaking.sentences(reacted ? TurnTaking.stripLead(answer.turn.replyText) : answer.turn.replyText))
    }

    /// `.some(value)` if the task finishes within `seconds`, else nil (the task keeps running).
    private func first<T: Sendable>(_ task: Task<T, Never>, within seconds: Double) async -> T?? {
        await withTaskGroup(of: T??.self) { group in
            group.addTask { .some(await task.value) }
            group.addTask { try? await Task.sleep(for: .seconds(seconds)); return nil }
            let r = await group.next() ?? nil
            group.cancelAll()
            return r
        }
    }

    // MARK: Speaking

    private func remember(_ line: String) {
        recentlySpoken = Array((recentlySpoken + [line]).suffix(3))
    }

    private func say(_ line: String) async {
        remember(line)
        state = .speaking
        await output.speak(line)
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

    var input: SpeechInput? { get { lock.withLock { _input } } set { lock.withLock { _input = newValue } } }
    var wake: WakeWordDetector? { get { lock.withLock { _wake } } set { lock.withLock { _wake = newValue } } }

    func prepare(from format: AVAudioFormat) { converter = AVAudioConverter(from: format, to: target) }

    func push(_ buffer: AVAudioPCMBuffer) {
        let input = self.input
        input?.feed(native: buffer)
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

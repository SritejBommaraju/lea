import SwiftUI

/// The speaker toggle on the scan and report screens (remembered).
struct VoiceMuteButton: View {
    var onCamera = false
    @State private var voice = LeaVoice.shared

    var body: some View {
        Button { voice.muted.toggle() } label: {
            Image(systemName: voice.muted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(RoundIconButtonStyle(onCamera: onCamera))
        .accessibilityLabel(voice.muted ? "Turn Lea's voice on" : "Mute Lea")
    }
}

/// Toolbar version of the toggle (the report's navigation bar).
struct VoiceMuteToolbarButton: View {
    @State private var voice = LeaVoice.shared

    var body: some View {
        Button { voice.muted.toggle() } label: {
            Image(systemName: voice.muted ? "speaker.slash" : "speaker.wave.2")
        }
        .accessibilityLabel(voice.muted ? "Turn Lea's voice on" : "Mute Lea")
    }
}

// MARK: - Ask Lea (hold to talk) on the report

/// Hold the button, ask about this scan, let go: the question goes to the gateway (`/v1/voice/ask`), and Lea's
/// answer is shown and spoken. Grounded in this scan's report and the person's own description only.
struct AskLeaBar: View {
    let scanID: String
    let gateway: GatewayClient
    let diag: Diag

    enum Phase: Equatable { case idle, listening, thinking, answered, failed(String) }
    @State private var phase: Phase = .idle
    @State private var question = ""
    @State private var answer = ""
    @State private var held = false
    @State private var recorder = VoiceRecorder()
    @State private var voice = LeaVoice.shared
    @State private var task: Task<Void, Never>?

    var body: some View {
        VStack(spacing: Spacing.s) {
            if !question.isEmpty || !answer.isEmpty || isFailed {
                exchange.transition(.move(edge: .bottom).combined(with: .opacity))
            }
            holdButton
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.top, Spacing.s)
        .padding(.bottom, Spacing.xs)
        .background(.bar)
        .animation(Motion.standard, value: phase)
    }

    private var isFailed: Bool { if case .failed = phase { true } else { false } }

    private var exchange: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if !question.isEmpty {
                Label { Text(question) } icon: { Image(systemName: "person.fill") }
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            if case .failed(let m) = phase {
                Label { Text(m) } icon: { Image(systemName: "exclamationmark.bubble") }
                    .font(.subheadline).foregroundStyle(Palette.caution)
            } else if phase == .thinking {
                Label { Text("Thinking…") } icon: { ProgressView().controlSize(.small) }
                    .font(.subheadline).foregroundStyle(.secondary)
            } else if !answer.isEmpty {
                Label { Text(answer) } icon: { Image(systemName: "waveform") }
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Spacing.m)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private var holdButton: some View {
        HStack(spacing: Spacing.s) {
            Image(systemName: phase == .listening ? "waveform" : "mic.fill")
                .symbolEffect(.variableColor.iterative, isActive: phase == .listening)
            Text(phase == .listening ? "Listening… let go to ask" : "Hold to ask Lea")
        }
        .font(.buttonLabel)
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, minHeight: 56)
        .background(phase == .listening ? Palette.accent.opacity(0.8) : Palette.accentFill, in: Capsule())
        .scaleEffect(held ? 0.97 : 1)
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in if !held { held = true; press() } }
            .onEnded { _ in held = false; release() })
        .sensoryFeedback(.impact(weight: .light), trigger: held) { _, new in new }   // on press only
        .accessibilityLabel("Ask Lea")
        .accessibilityHint("Hold, ask a question about this scan, then let go")
        .accessibilityAddTraits(.isButton)
    }

    private func press() {
        task?.cancel()
        voice.stop()
        phase = .listening
        Task {
            if await !recorder.start() {
                phase = .failed("Lea can't hear you: allow the microphone in Settings.")
            } else if !held {
                release()   // let go before the recorder was ready
            }
        }
    }

    private func release() {
        guard phase == .listening else { return }
        guard let audio = recorder.stop() else {
            phase = question.isEmpty && answer.isEmpty ? .idle : .answered
            return
        }
        let released = Date()
        phase = .thinking
        task = Task {
            do {
                let cartesia = voice.status?.cartesia == true
                var spoken: String?
                if !cartesia { spoken = await recorder.transcribeOnDevice() }
                let out = try await gateway.ask(scanID: scanID, audio: cartesia ? audio : nil, question: spoken,
                                                inlineAudio: false)
                if Task.isCancelled { return }
                question = out.transcript
                answer = out.answerText
                phase = .answered
                let textMs = Int(Date().timeIntervalSince(released) * 1000)
                voice.say(out.answerText) { [diag] ms, source in
                    diag.log("voice_ask", ["release_to_audio_ms": textMs + ms, "release_to_text_ms": textMs,
                                           "source": source, "server_ms": out.timings])
                }
            } catch {
                phase = .failed(Self.message(error))
            }
        }
    }

    static func message(_ error: Error) -> String {
        if case GatewayError.http(503, _) = error { return "Answers are off on the server right now." }
        if case GatewayError.http(404, _) = error { return "This scan has no report on the server yet." }
        return "Lea couldn't answer: \(error.localizedDescription)"
    }
}

// MARK: - Dictating what's wrong on the home screen

/// A mic in the "What's wrong?" field: hold to dictate. Words come from the gateway (`/v1/voice/stt`) when it has a
/// voice key, else from the phone's own recognizer; they are added to the field.
struct StoryMicButton: View {
    @Binding var text: String
    let gateway: GatewayClient?

    @State private var recorder = VoiceRecorder()
    @State private var held = false
    @State private var listening = false
    @State private var working = false

    var body: some View {
        ZStack {
            if working {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: listening ? "waveform" : "mic.fill")
                    .symbolEffect(.variableColor.iterative, isActive: listening)
                    .foregroundStyle(Palette.accent)
            }
        }
        .frame(width: 36, height: 36)
        // A visible disc: talking instead of typing is the point of the field.
        .background(Palette.accent.opacity(listening ? 0.22 : 0.12), in: Circle())
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in if !held { held = true; press() } }
            .onEnded { _ in held = false; release() })
        .sensoryFeedback(.impact(weight: .light), trigger: listening) { _, new in new }   // on press only
        .accessibilityLabel("Dictate what's wrong")
        .accessibilityHint("Hold and speak, then let go")
        .accessibilityAddTraits(.isButton)
    }

    private func press() {
        listening = true
        Task {
            if await !recorder.start() { listening = false } else if !held { release() }
        }
    }

    private func release() {
        guard listening else { return }
        listening = false
        guard let audio = recorder.stop() else { return }
        working = true
        Task {
            defer { working = false }
            var words = ""
            if LeaVoice.shared.status?.cartesia == true, let gateway {
                words = (try? await gateway.stt(audio: audio)) ?? ""
            }
            if words.isEmpty { words = await recorder.transcribeOnDevice() }
            words = words.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !words.isEmpty else { return }
            text = text.trimmingCharacters(in: .whitespaces).isEmpty ? words : text + " " + words
        }
    }
}

// MARK: - Talking with Lea (conversation)

/// Captions for both sides, a "Take a closer photo" style action when Lea offers one, and the talk button: hold to
/// talk (recorded, Cartesia's recognizer), or tap once to talk hands-free (the phone's recognizer; the turn ends
/// when you stop talking, and Lea listens for a follow-up after she answers). "Hey Lea" talks to this panel too.
struct ConversationPanel: View {
    @State private var convo: LeaConversation
    let onAction: (String) -> Void
    var compact = false

    @State private var wake = WakeListener.shared
    @State private var pressedAt: Date?
    @State private var typed = ""

    init(scanID: String?, gateway: GatewayClient, diag: Diag?, sessionID: @escaping () -> String? = { nil },
         compact: Bool = false, afterTurn: (@MainActor () -> Void)? = nil, onAction: @escaping (String) -> Void) {
        let c = LeaConversation(scanID: scanID, gateway: gateway, diag: diag)
        c.sessionID = sessionID
        c.afterTurn = afterTurn
        c.acceptOffer = onAction   // "yes" to the offer on screen is the same as tapping it
        _convo = State(initialValue: c)
        self.compact = compact
        self.onAction = onAction
    }

    var body: some View {
        VStack(spacing: Spacing.s) {
            if !convo.turns.isEmpty || !convo.liveCaption.isEmpty || isFailed { captions }
            if let action = convo.action, action.kind != "walkthrough" {
                Button { onAction(action.kind) } label: {
                    Label(action.label, systemImage: action.kind == "start_scan" ? Symbol.scan : Symbol.closeup)
                }
                .buttonStyle(.primary(.quiet))
                .transition(.opacity)
            }
            if !compact {
                // Detail that is easier to type (a model number, an error code): the same turn as saying it.
                FieldWell(symbol: "keyboard") {
                    TextField("Type to Lea", text: $typed)
                        .submitLabel(.send)
                        .onSubmit(sendTyped)
                }
            }
            HStack(spacing: Spacing.s) {
                talkButton
                WakeIndicator()
            }
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.top, Spacing.s)
        .padding(.bottom, Spacing.xs)
        .background(.bar)
        .animation(Motion.standard, value: convo.phase)
        .animation(Motion.standard, value: convo.turns.count)
        .onAppear { convo.appeared() }
        // The person asked for it in so many words: the tour starts on its own (after Lea's "I'll walk you through it").
        .onChange(of: convo.actionKind) { _, kind in if kind == "walkthrough" { onAction("walkthrough") } }
        .task {
            WakeListener.shared.capture(false)   // back from a scan: listening resumes here for sure
            await convo.loadHistory()
            if let first = wake.pendingUtterance {
                wake.pendingUtterance = nil
                convo.heard(first)
            }
        }
        .onDisappear {
            convo.disappeared()
            if convo.visible == 0 { convo.stopLea() }
        }
    }

    private var isFailed: Bool { if case .failed = convo.phase { true } else { false } }

    private func sendTyped() {
        let words = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { return }
        typed = ""
        convo.typed(words)
    }

    private var captions: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    ForEach(convo.turns.suffix(compact ? 4 : 12)) { t in
                        CaptionRow(mine: t.mine, text: t.text).id(t.id)
                    }
                    if !convo.liveCaption.isEmpty {
                        CaptionRow(mine: true, text: convo.liveCaption, live: true).id("live")
                    }
                    if convo.phase == .thinking {
                        Label { Text("Thinking…") } icon: { ProgressView().controlSize(.small) }
                            .font(.subheadline).foregroundStyle(.secondary).id("thinking")
                    }
                    if case .failed(let m) = convo.phase {
                        Label { Text(m) } icon: { Image(systemName: "exclamationmark.bubble") }
                            .font(.subheadline).foregroundStyle(Palette.caution).id("failed")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(Spacing.m)
            }
            .frame(maxHeight: compact ? 150 : 220)
            .fixedSize(horizontal: false, vertical: true)
            .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: 0.5))
            .onChange(of: convo.turns.last?.text) { _, _ in withAnimation { proxy.scrollTo(convo.turns.last?.id, anchor: .bottom) } }
            .onChange(of: convo.liveCaption) { _, _ in proxy.scrollTo("live", anchor: .bottom) }
        }
    }

    private var talkButton: some View {
        let listening = convo.phase == .listening || wake.mode == .followUp
        return HStack(spacing: Spacing.s) {
            Image(systemName: listening ? "waveform" : (convo.phase == .speaking ? "speaker.wave.2.fill" : "mic.fill"))
                .symbolEffect(.variableColor.iterative, isActive: listening || convo.phase == .speaking)
            Text(listening ? "Listening…" : (convo.phase == .speaking ? "Tap to interrupt" : "Talk to Lea"))
        }
        .font(.buttonLabel)
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, minHeight: 56)
        // The same capsule as Start scan and Done: one shape for "the one thing to press".
        .background(listening ? Palette.accent.opacity(0.8) : Palette.accentFill, in: Capsule())
        .scaleEffect(pressedAt != nil ? 0.97 : 1)
        .animation(Motion.press, value: pressedAt != nil)
        .contentShape(Rectangle())
        .holdOrTapToTalk(convo, pressedAt: $pressedAt) {
            // A tap: hands-free. The phone listens; the turn ends when you stop talking.
            convo.handsFree = true
            wake.listenForFollowUp()
        }
        .sensoryFeedback(.impact(weight: .light), trigger: pressedAt != nil) { _, new in new }   // on press only
        .accessibilityLabel("Talk to Lea")
        .accessibilityHint("Hold to ask, or tap once to talk hands-free")
        .accessibilityAddTraits(.isButton)
    }
}

private struct CaptionRow: View {
    let mine: Bool
    let text: String
    var live = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
            Image(systemName: mine ? "person.fill" : "waveform").imageScale(.small)
                .foregroundStyle(mine ? AnyShapeStyle(.secondary) : AnyShapeStyle(Palette.accent))
                .accessibilityHidden(true)
            Text(text)
                .font(mine ? .subheadline : .body)
                .foregroundStyle(mine ? AnyShapeStyle(.secondary) : AnyShapeStyle(.primary))
                .opacity(live ? 0.7 : 1)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel((mine ? "You: " : "Lea: ") + text)
    }
}

/// A small ear while "Hey Lea" is listening (the system's mic dot shows too).
struct WakeIndicator: View {
    var onCamera = false
    @State private var wake = WakeListener.shared

    var body: some View {
        if wake.mode != .off {
            Image(systemName: wake.mode == .followUp ? "ear.badge.waveform" : "ear")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(onCamera ? AnyShapeStyle(.white) : AnyShapeStyle(Palette.accent))
                .frame(width: 44, height: 44)
                .background(onCamera ? AnyShapeStyle(Color.black.opacity(0.45)) : AnyShapeStyle(Palette.surface), in: Circle())
                .symbolEffect(.pulse, isActive: wake.mode == .followUp)
                .accessibilityLabel(wake.mode == .followUp ? "Lea is listening" : "Listening for Hey Lea")
                .transition(.opacity)
        }
    }
}

/// The home screen's conversation, before any scan: opened by "Hey Lea" or the Talk button. Lea works out what the
/// object is (what the person says, plus an optional photo of the label), says what public sources report about
/// it, and the scan then starts in the same session (Voice/PreScan), so the conversation carries on at the report.
struct HomeConversationSheet: View {
    let gateway: GatewayClient
    let startScan: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var pre = PreScan.shared
    @State private var camera = false
    @State private var spokenIdentity: String?
    @State private var spokenBrief: ProductBrief?
    @State private var saidLookingUp = false

    var body: some View {
        NavigationStack {
            VStack(spacing: Spacing.m) {
                Text("Tell Lea what it is and what's wrong. She'll work out the exact product before you scan.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Spacing.gutter)
                if let brief = pre.live?.brief { briefCard(brief).padding(.horizontal, Spacing.gutter) }
                HStack(spacing: Spacing.s) {
                    Button { camera = true } label: {
                        Label(labelTitle, systemImage: pre.label == .sent ? "checkmark" : "camera.viewfinder")
                    }
                    .buttonStyle(.primary(.quiet))
                    .disabled(pre.sessionID == nil || pre.label == .sending)
                    Button { begin() } label: { Label("Start scan", systemImage: Symbol.scan) }
                        .buttonStyle(.primary(.quiet))
                }
                .padding(.horizontal, Spacing.gutter)
                if case .failed(let m) = pre.label {
                    Text(m).font(.footnote).foregroundStyle(Palette.caution).padding(.horizontal, Spacing.gutter)
                }
                Spacer(minLength: 0)
                ConversationPanel(scanID: pre.scanID, gateway: gateway, diag: WakeListener.diag,
                                  sessionID: { PreScan.shared.sessionID }, afterTurn: lookingUp) { kind in
                    if kind == "start_scan" { begin() }
                }
                .id(pre.scanID)
            }
            .padding(.top, Spacing.s)
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle("Lea").navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() }.fontWeight(.semibold) }
            .fullScreenCover(isPresented: $camera) {
                CameraPicker { image in
                    camera = false
                    if let image { Task { await pre.sendLabel(image, gateway: gateway) } }
                }
                .ignoresSafeArea()
            }
            .task {
                await pre.begin(gateway: gateway)
                // Opened by "Hey Siri, hey lea": Lea speaks first, then listens (hands-free).
                if IntentInbox.shared.takeGreeting() { LeaConversation.active?.leaSays("Hey, what's up?") }
            }
            // What the backend works out is said once, in Lea's voice, and shown in the conversation.
            .onChange(of: pre.live?.identity?.words) { _, words in
                guard let words, words != spokenIdentity, pre.live?.brief == nil else { return }
                spokenIdentity = words
                LeaConversation.active?.leaSays("This looks like \(words).")
            }
            .onChange(of: pre.live?.brief) { _, brief in
                guard let brief, brief != spokenBrief else { return }
                spokenBrief = brief
                LeaConversation.active?.leaSays(brief.spoken)
            }
        }
        .presentationDetents([.large])
    }

    /// After the first answer, while the brief is being researched (about half a minute): say so, once.
    private func lookingUp() {
        guard !saidLookingUp, pre.live?.brief == nil, pre.sessionID != nil else { return }
        saidLookingUp = true
        LeaConversation.active?.leaSays("Let me look that up while we talk. It takes about half a minute.")
    }

    private var labelTitle: String {
        switch pre.label {
        case .sending: "Sending…"
        case .sent: "Label sent"
        default: "Show me the label"
        }
    }

    private func briefCard(_ b: ProductBrief) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Text([b.brand, b.model, b.name].compactMap { $0 }.joined(separator: " · ")).font(.headline)
            ForEach(Array(b.knownIssues.prefix(3).enumerated()), id: \.offset) { _, issue in
                Label(issue, systemImage: "exclamationmark.circle").font(.footnote).foregroundStyle(.secondary)
            }
            if !b.sources.isEmpty {
                Text("From \(b.sources.prefix(2).joined(separator: ", "))").font(.caption).foregroundStyle(.tertiary).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Spacing.m)
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
    }

    private func begin() {
        let said = LeaConversation.active?.turns.filter(\.mine).map(\.text).joined(separator: " ") ?? ""
        dismiss()
        startScan(said)
    }
}

// MARK: - Talking with Lea while scanning

/// Lea over the camera while scanning: the same conversation as this scan's report (it carries on there), one line
/// of captions, an ear that turns continuous hands-free on and off, and the mic (hold to ask, tap to talk
/// hands-free). Swipe up on it to start talking hands-free. Each turn tells Lea what the scan screen shows now
/// (`live`), so she can coach while you scan. Small, so the camera stays clear.
struct ScanTalkBar: View {
    @State private var convo: LeaConversation
    @State private var wake = WakeListener.shared
    @State private var pressedAt: Date?

    init(scanID: String, gateway: GatewayClient, diag: Diag?, sessionID: @escaping () -> String?,
         live: @escaping () -> [String: String], eyes: LeaEyes? = nil) {
        let c = LeaConversation(scanID: scanID, gateway: gateway, diag: diag)
        c.sessionID = sessionID
        c.live = live
        c.eyes = eyes
        _convo = State(initialValue: c)
    }

    var body: some View {
        HStack(spacing: Spacing.s) {
            Button { wake.continuous.toggle() } label: {
                Image(systemName: wake.continuous ? "ear.badge.waveform" : "ear")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(wake.continuous ? Palette.cameraAccent : .white)
                    .frame(width: 36, height: 36)
                    .symbolEffect(.pulse, isActive: wake.mode == .followUp)
            }
            .accessibilityLabel(wake.continuous ? "Hands-free on" : "Hands-free off")
            Text(caption)
                .font(.footnote.weight(.medium))
                .foregroundStyle(isFailed ? Palette.caution : .white)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .animation(Motion.fade, value: caption)
            Image(systemName: listening ? "waveform.circle.fill" : (convo.phase == .speaking ? "speaker.wave.2.circle.fill" : "mic.circle.fill"))
                .font(.system(size: 34))
                .foregroundStyle(pressedAt != nil ? Palette.danger : .white)
                .symbolEffect(.variableColor.iterative, isActive: listening || convo.phase == .speaking)
                .holdOrTapToTalk(convo, pressedAt: $pressedAt) { talkHandsFree() }
                .accessibilityLabel("Talk to Lea")
                .accessibilityHint("Hold to ask, or tap once to talk hands-free")
                .accessibilityAddTraits(.isButton)
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.xs)
        .background(.black.opacity(0.55), in: Capsule())
        .contentShape(Capsule())
        .gesture(DragGesture(minimumDistance: 20).onEnded { if $0.translation.height < -30 { talkHandsFree() } })
        .environment(\.colorScheme, .dark)
        .onAppear {
            convo.appeared()
            if wake.continuous { wake.listenForFollowUp() }   // hands-free carries on into the scan
        }
        .task { await convo.loadHistory() }
        .onDisappear {
            convo.disappeared()
            if convo.visible == 0 { convo.stopLea() }
        }
    }

    private var listening: Bool { convo.phase == .listening || wake.mode == .followUp }
    private var isFailed: Bool { if case .failed = convo.phase { true } else { false } }

    private var caption: String {
        if case .failed(let m) = convo.phase { return m }
        if !convo.liveCaption.isEmpty { return convo.liveCaption }
        if convo.phase == .thinking { return "Thinking…" }
        if let last = convo.turns.last(where: { !$0.mine }), convo.phase == .speaking || !listening { return last.text }
        return listening ? "Lea is listening. Ask anything while you scan." : "Swipe up and talk, or hold the mic."
    }

    /// Swipe up or tap: Lea listens hands-free and keeps listening after each answer.
    private func talkHandsFree() {
        convo.handsFree = true
        wake.continuous = true
        wake.listenForFollowUp()
    }
}

// MARK: - Hold to ask, tap for hands-free

extension View {
    /// The talk button's gesture (the conversation panel and the scan bar): pressing stops Lea at once (barge-in) and
    /// records; letting go after a hold sends the question; a tap (under 0.35 s) drops the recording and runs `tap`.
    func holdOrTapToTalk(_ convo: LeaConversation, pressedAt: Binding<Date?>, tap: @escaping () -> Void) -> some View {
        gesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in
                guard pressedAt.wrappedValue == nil else { return }
                pressedAt.wrappedValue = Date()
                convo.pressToTalk()
            }
            .onEnded { _ in
                let held = pressedAt.wrappedValue.map { Date().timeIntervalSince($0) } ?? 0
                pressedAt.wrappedValue = nil
                if held < 0.35 { convo.cancelHold(); tap() } else { convo.releaseToTalk() }
            })
    }
}

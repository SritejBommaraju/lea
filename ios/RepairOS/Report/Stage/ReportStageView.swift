import SwiftUI

/// The report, centred on the 3D scan and Lea (redesign plan §1.1): the scan on the flat stage (about 62% of the
/// screen) with Lea's caption strip on it, and the report sheet below. When Lea moves the model while she answers
/// (LeaStage), the sheet scrolls back to the stage if it was mostly off screen, and the strip shows the sentence she
/// is on, prefixed by the part she is showing.
///
/// The landing moment (`fresh`): the markers pop in 1, 2, 3 (ModelView), the sheet rises and the verdict haptic
/// fires on the same frame, then the rows fade in. Opening a saved report shows it at once, with no haptic.
/// Reduce Motion: every step is a fade.
struct ReportStageView: View {
    let report: ReportModel
    var symptom: String?
    var model: ModelSource = .none
    var sessionDir: URL?
    var space: ObjectSpace?
    var openFull: (() -> Void)?
    var fix: FixIt?
    var fixSearching = false
    /// The parts search was skipped (spend cap): FixItCard's quiet line.
    var fixUnavailable = false
    /// A new report just arrived (play the landing moment and its haptic); false when reopening a saved one.
    var fresh = false
    /// Bumped when Lea's first sound for this report plays (ReportView.speakLanded): the verdict lands on that frame.
    var voiceStarted = 0
    /// Bumped by Lea's `walkthrough` voice action ("Lea, walk me through it"): plays the tour once she has finished
    /// saying she will.
    var walkthrough = 0
    /// Lea's latest move of the 3D model while she answers (LeaStage): focus and ring a part, look from a side, zoom.
    var leaCue: LeaStageCue?
    /// The hero on the stage. `.automatic` follows `model` (3D view or the photo hero); a 3D recreation plugs in
    /// with `.custom` (see `StageHeroContext`) without touching anything else here.
    var hero: StageHero = .automatic
    /// Lea's conversation for the caption strip (nil when she can't be asked: not paired).
    @ObservedOptional var captions: LeaCaptionFeed? = nil
    /// What the object is ("Category · Brand Model"): the person's correction or the backend's identification. Shown
    /// on the stage whatever the hero (a photo, a scan, or nothing usable in 3D). nil falls back to the report's own.
    var identity: String? = nil
    /// Opens the meter-reading sheet (nil when there is no gateway to send it to, e.g. Demo replay).
    var addMeterReading: (() -> Void)? = nil
    /// Opens "Fix it with Grain" (nil: not judged against a design, or no gateway).
    var askDesign: (() -> Void)? = nil
    /// Meter readings sent from this report so far.
    var meterReadingsSent = 0

    @Environment(\.cropSource) private var cropSource
    @State private var bottomZoom: BottomZoom?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOver
    @State var focus: String?
    @State private var sheet: ReportModel.Problem?
    @State private var showOthers = false
    @State private var showTranscript = false
    @State private var handoffZoom: HandoffZoom?
    /// The part the current sentence is about (from Lea's `show` / `point`), for the strip's prefix.
    @State private var captionPart: LeaCaptionStrip.Part?
    /// The strip hides after a quiet spell (the transcript keeps everything).
    @State private var captionIdle = false
    /// How far the sheet is scrolled, and the screen height (to know when the stage is mostly off screen).
    @State private var scrolled: CGFloat = 0
    @State private var screenHeight: CGFloat = 800
    /// 0 nothing, 1 stage, 2 sheet (verdict), 3 rows.
    @State private var phase = 0
    @StateObject private var probe = StageProbe()
    /// The caption strip's measured height (it grows with Dynamic Type and long sentences); the hero leaves that room.
    @State private var captionHeight: CGFloat = ReportStageView.captionBand
    /// "Walk me through it": the guided tour of this report on the hero (Walkthrough, WalkthroughDeck).
    @StateObject private var tour = WalkthroughPlayer()
    /// Rings Lea put on parts that are not numbered problems (a part with no issue, a spot read from the photos), for
    /// a few seconds; and her camera move for the hero.
    @State private var leaPins: [ModelPin] = []
    /// The name on the part Lea is showing (a finding's part, or a `point`'s label).
    @State private var leaLabel: (id: String, text: String)?
    @State private var heroCue: LeaStageCue?
    static let leaRingSeconds: Double = 6
    /// How much of the screen the stage takes (StageDetents): it grows while Lea answers.
    @State private var stageLayout = StageDetents()
    /// The window's top safe area: the stage lays out under the report's top bar, whatever the phone.
    @State private var topInset: CGFloat = 48
    /// Lea's camera move is under way (about a second after her cue): the name chip keeps out of the way.
    @State private var cameraMoving = false
    /// The stage's size, for placing the photo card clear of the part.
    @State private var stageSize: CGSize = .zero
    /// Counts Lea's first sounds seen (from `voiceStarted`), read live by the landing task.
    @State private var heard = 0
    /// The 3D scan is on screen: the landing waits for it (at most `maxWait`), so markers and verdict arrive on it.
    @State private var modelReady = false
    private static let maxWait: Double = 2.5
    /// Longest the verdict waits for Lea's first sound after the markers have landed.
    private static let voiceWait: Double = 0.35

    // MARK: Derived

    private var pins: [ModelPin] {
        let all = StageMarks.pins(report) + leaPins
        // The part Lea is showing carries its name: the hero draws it as a label out along the surface.
        guard let leaLabel else { return all }
        return all.map { p in
            var p = p
            if p.id == leaLabel.id { p.label = leaLabel.text }
            return p
        }
    }

    /// The numbered findings and setup rows only (Lea's rings are not problems).
    private var findingPins: [ModelPin] { pins.filter { $0.ringRadius == nil } }

    private var numberedPhotos: [PhotoHero.Mark] { StageMarks.photoMarks(report) }

    /// Lower-ranked and cosmetic findings; setup rows have their own section ("Setup to check").
    private var others: [ReportModel.Problem] { report.otherIssues + report.cosmetic }
    private var all: [ReportModel.Problem] { report.mostLikely + report.otherIssues + report.setup + report.cosmetic }
    private var isPhotoHero: Bool { if case .photos = model { true } else { false } }
    private var isCustomHero: Bool { if case .custom = hero { true } else { false } }
    private var landingKey: String { "\(report.verdict)|\(report.mostLikely.map(\.id))" }

    // MARK: Body

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                    stage.id("stage")
                    sheetBody(proxy)
                        .padding(.top, -Self.overlap)
                        .opacity(tour.active || stageLayout.detent == .expanded ? 0 : 1)
                        // While the stage owns the screen the sheet only peeks: pulling it up brings the report back
                        // (and it stays for the rest of the answer).
                        .simultaneousGesture(DragGesture(minimumDistance: 16).onEnded { v in
                            guard !stageLayout.sheetScrolls, v.translation.height < -30 else { return }
                            setLayout { $0.userPulledSheetUp() }
                        })
                }
                .reportsScrollOffset()
            }
            .bounceBasedOnSize()
            .stageScrollMetrics(scrolled: $scrolled, height: $screenHeight)
            // Lea moved the model while the stage was mostly scrolled away: back to the stage first (L9).
            .onValueChange(of: leaCue) { _, cue in
                guard cue != nil, stageLayout.followsLea, scrolled > stageHeight * 0.4 else { return }
                withAnimation(Motion.pick(Motion.standard, reduceMotion: reduceMotion)) { proxy.scrollTo("stage", anchor: .top) }
            }
            // An answer starting: the stage takes most of the screen, from the top (the sheet drops to a peek).
            .onValueChange(of: stageLayout.detent) { old, new in
                guard old == .report, new != .report else { return }
                withAnimation(StageDetents.animation(reduceMotion: reduceMotion)) { proxy.scrollTo("stage", anchor: .top) }
            }
            // The tour has the whole screen: the stage fills it and the sheet waits below. While Lea answers the stage
            // owns the screen too, and the sheet waits for a pull.
            .scrollDisabled(tour.active || !stageLayout.sheetScrolls)
            .onValueChange(of: tour.active) { _, on in
                if on { withAnimation(Motion.pick(Motion.standard, reduceMotion: reduceMotion)) { proxy.scrollTo("stage", anchor: .top) } }
            }
            // Pulling down past the top shows more stage, never a strip of canvas.
            .background(alignment: .top) { StageBackground().frame(height: 700) }
            .background(tour.active ? Palette.stage : Palette.canvas)
            .ignoresSafeArea(edges: .top)
            .navigationDestination(for: String.self) { id in
                if let p = all.first(where: { $0.id == id }) {
                    ProblemDetailView(problems: [p], stillsDir: sessionDir?.appending(path: "stills"), numbers: markerNumbers)
                }
            }
        }
        .toolbarBackground(.hidden, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .haptic(trigger: phase) { _, new in
            guard new == 2, fresh else { return nil }
            return report.verdict == .potentialIssues ? Haptic.verdictProblems : (report.verdict == .looksGood ? Haptic.verdictClear : nil)
        }
        .haptic(Haptic.select, trigger: focus) { _, new in new != nil }
        .task(id: landingKey) { await land() }
        .onValueChange(of: voiceStarted) { heard += 1 }
        .onValueChange(of: walkthrough) { startTour(afterLea: true) }
        .onValueChange(of: leaCue) { _, cue in applyLea(cue) }
        // Lea answering: her first sentence (or first move) grows the stage; the answer ending leaves it be.
        .onValueChange(of: captions?.speaking ?? false) { _, speaking in
            if speaking { answerStarted() } else if !(captions?.thinking ?? false) { stageLayout.answerEnded() }
        }
        .onValueChange(of: heroCue) { _, cue in
            guard cue != nil else { return }
            cameraMoving = true
            Task {
                try? await Task.sleep(for: .seconds(Motion.Scene.flyRange.upperBound + 0.3))
                cameraMoving = false
            }
        }
        .onAppear { topInset = StageMetrics.windowTopInset }
        // The camera left the part Lea showed (a touch on the stage, the idle drift, a reset): her ring and label go
        // with it, so nothing stale stays drawn after a tap elsewhere.
        .onValueChange(of: probe.landed) { old, new in
            guard old != nil, new == nil else { return }
            leaPins = []
            leaLabel = nil
        }
        .haptic(Haptic.leaShow, trigger: heroCue) { _, new in new != nil }
        .onValueChange(of: captions?.question) { captionPart = nil }
        .task(id: captions?.revision) {
            // Hidden after 8 s of quiet once Lea has finished (the transcript keeps everything).
            captionIdle = false
            guard let c = captions, !c.thinking, !c.speaking else { return }
            try? await Task.sleep(for: .seconds(8))
            if !Task.isCancelled { withAnimation(Motion.fade) { captionIdle = true } }
        }
        .sheet(isPresented: $showTranscript) { if let captions { LeaTranscriptSheet(feed: captions) } }
        .onDisappear { tour.stop() }
        .sheet(isPresented: $showOthers) {
            OtherFindingsSheet(problems: others) { p in
                showOthers = false
                Task { try? await Task.sleep(for: .milliseconds(350)); sheet = p }
            }
            .environment(\.cropSource, cropSource)
        }
        .sheet(item: $sheet) { p in
            // Previous and next step through the numbered problems in marker order, in place.
            let list = detailList.contains { $0.id == p.id } ? detailList : [p]
            NavigationStack {
                ProblemDetailView(problems: list, index: list.firstIndex { $0.id == p.id } ?? 0,
                                  stillsDir: sessionDir?.appending(path: "stills"), numbers: markerNumbers,
                                  onDone: { sheet = nil })
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        // Leaving the report ends Lea's rings and labels with it: nothing stale is drawn when it opens again.
        .onDisappear { leaPins = []; leaLabel = nil; heroCue = nil }
    }

    /// "Add a meter reading", and a line saying how many were sent. A meter reading is the only proof (I1, I2).
    private func meterReadingRow(_ open: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            Button(action: open) { Label("Add a meter reading", systemImage: "gauge.with.dots.needle.50percent") }
                .buttonStyle(.textAction)
            if meterReadingsSent > 0 {
                Text(meterReadingsSent == 1 ? "1 meter reading sent. The report updates when it is judged."
                                            : "\(meterReadingsSent) meter readings sent. The report updates when they are judged.")
                    .font(.subheadline).foregroundStyle(Palette.secondary)
            }
        }
    }

    /// The numbered problems and setup rows, in marker order (the detail's previous / next).
    private var detailList: [ReportModel.Problem] { report.mostLikely + report.setup }
    private var markerNumbers: [String: Int] {
        Dictionary(detailList.enumerated().map { ($0.element.id, $0.offset + 1) }, uniquingKeysWith: { a, _ in a })
    }

    private var objectName: String? { identity?.trimmedOrNil ?? report.objectWords }

    /// The photo for the part the camera has landed on: the one Lea's answer sent for it, else the finding's own crop.
    private var handoffPhoto: String? {
        guard let id = probe.landed else { return nil }
        let raw = id.hasPrefix("lea:") ? String(id.dropFirst(4)) : id
        if let turn = captions?.turns.last(where: { !$0.mine }), let sha = turn.photoFor[raw] ?? turn.photoFor[id] { return sha }
        return all.first { $0.id == raw }?.crops.first
    }

    /// The stage's share of the screen (the report centres on the model and Lea; more of it while she answers).
    private var stageHeight: CGFloat { StageDetents.stageHeight(stageLayout.detent, container: screenHeight, touring: tour.active) }

    /// Changes the stage's size with its own spring (a cut under Reduce Motion).
    private func setLayout(_ change: (inout StageDetents) -> Void) {
        withAnimation(StageDetents.animation(reduceMotion: reduceMotion)) { change(&stageLayout) }
    }

    private func answerStarted() {
        guard !tour.active else { return }
        setLayout { $0.answerStarted() }
        #if DEBUG
        // Design review only: the expanded stage a few seconds into the replayed answer.
        if CommandLine.arguments.contains("--ui-stage-expand") {
            Task { try? await Task.sleep(for: .seconds(4)); setLayout { $0.expand() } }
        }
        #endif
    }

    /// Where the photo card may go: the stage below the top bar, above the bottom controls (the caption strip and
    /// the round recentre control), inset by the stage margin.
    private var handoffArea: CGRect {
        let top = topClear + Spacing.s
        let bottom = Self.overlap + Spacing.m + band + 52
        return CGRect(x: Spacing.l, y: top, width: max(0, stageSize.width - 2 * Spacing.l),
                      height: max(0, stageSize.height - top - bottom))
    }

    /// The part Lea's camera landed on, in stage points (the hero sits under the top bar, above the bottom band).
    private var landedOnStage: CGPoint? {
        guard let p = probe.landedPoint, stageSize.width > 0 else { return nil }
        let heroTop = topClear, heroHeight = stageSize.height - topClear - Self.overlap - band
        return CGPoint(x: p.x * stageSize.width, y: heroTop + p.y * max(heroHeight, 1))
    }

    /// Lea is busy on the stage (answering, the camera moving, a ring, label or photo showing): the name chip keeps
    /// out of the way until she is done.
    private var stageBusy: Bool {
        (captions?.speaking ?? false) || (captions?.thinking ?? false) || cameraMoving || !leaPins.isEmpty
            || leaLabel != nil || handoffPhoto != nil
    }

    /// The caption strip is on the stage: after the first turn, until a quiet spell, never during the walkthrough.
    private var showsCaptions: Bool { (captions?.started ?? false) && !captionIdle && !tour.active }
    /// Room the strip takes at the bottom of the stage.
    static let captionBand: CGFloat = 96

    /// How far the sheet rides up over the stage (its top corners are `Radius.sheet`).
    static let overlap: CGFloat = Radius.sheet
    /// Status bar + the report's top bar (44 pt controls) over the stage: the 3D view starts below them so the object
    /// is never under them, on any phone.
    private var topClear: CGFloat { topInset + Spacing.xs + 44 + Spacing.s }

    // MARK: Stage

    private var stage: some View {
        ZStack(alignment: .top) {
            StageBackground()
            heroView
            .padding(.bottom, Self.overlap)
            .overlay(alignment: .bottomLeading) {
                HStack(spacing: Spacing.s) {
                    if stageLayout.detent != .report {
                        // Back from the stage to the report (a swipe down on the caption strip does the same).
                        Button { setLayout { $0.collapse() } } label: { Label("Report", systemImage: "chevron.down") }
                            .buttonStyle(StageCapsuleButtonStyle())
                            .accessibilityHint("Shows the report under the scan again")
                    } else if hasTourContent, !stageLayout.answering {
                        // Not while Lea answers: nothing extra over the model then.
                        Button { startTour() } label: { Label("Walk me through it", systemImage: "play") }
                            .buttonStyle(StageCapsuleButtonStyle())
                            .accessibilityHint("Lea goes through the report on the scan, one problem at a time")
                    }
                }
                .padding(.leading, Spacing.l)
                .padding(.bottom, Self.overlap + Spacing.m + band)
                .opacity(tour.active ? 0 : 1)
                .allowsHitTesting(!tour.active)
            }
            .overlay {
                if tour.active {
                    // A tap on the object ends the tour; the cards below keep their own swipes and buttons.
                    Color.clear.contentShape(Rectangle())
                        .padding(.top, topClear)
                        .padding(.bottom, Self.deckHeight + Self.overlap)
                        .onTapGesture { tour.stop() }
                        .accessibilityHidden(true)
                }
            }
            .overlay(alignment: .bottom) {
                if tour.active {
                    WalkthroughDeck(player: tour)
                        .padding(.bottom, Self.overlap + Spacing.m)
                        .transition(.opacity.combined(with: .offset(y: reduceMotion ? 0 : 24)))
                }
            }
        }
        .overlay(alignment: .bottom) {
            if showsCaptions, let c = captions {
                VStack(alignment: .leading, spacing: Spacing.s) {
                    if stageLayout.detent == .expanded { LeaTranscriptOverlay(turns: c.turns) }
                    // A tap grows the stage to the full screen; on the full screen it opens the whole transcript.
                    LeaCaptionStrip(question: c.question, sentence: c.sentence, part: captionPart, thinking: c.thinking,
                                    failure: c.failure, tap: {
                                        if stageLayout.detent == .expanded { showTranscript = true } else { setLayout { $0.expand() } }
                                    })
                }
                    .padding(.horizontal, Spacing.l)
                    // Swipe up on the strip: the stage fills the screen; swipe down: back towards the report.
                    .simultaneousGesture(DragGesture(minimumDistance: 20).onEnded { v in
                        if v.translation.height < -30 { setLayout { $0.expand() } }
                        else if v.translation.height > 30 { setLayout { $0.collapse() } }
                    })
                    .accessibilityAction(named: stageLayout.detent == .expanded ? "Show the report" : "Bigger stage") {
                        setLayout { stageLayout.detent == .expanded ? $0.collapse() : $0.expand() }
                    }
                    // Its real height (Dynamic Type, a long sentence, the part line): the hero and the controls above
                    // it move up by that much, so the strip never covers them or gets cut off.
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { h in
                        captionHeight = max(Self.captionBand, h + Spacing.s)
                    }
                    .padding(.bottom, Self.overlap + Spacing.s)
                    .transition(.opacity)
            }
        }
        .overlay(alignment: .topLeading) {
            // The proof hand-off: when the camera lands on the part, its real photo (ringed by the gateway, or the
            // finding's own crop) rises in, in the stage corner farthest from the part (re-picked when the camera
            // settles, with a crossfade), so it never covers the part, its ring or its label; it recedes when the
            // camera moves on.
            if let sha = handoffPhoto, !tour.active {
                let place = HandoffPlacement.pick(part: landedOnStage, in: handoffArea)
                let frame = HandoffPlacement.rect(place.corner, side: place.side, in: handoffArea)
                Button { handoffZoom = HandoffZoom(sha: sha) } label: {
                    CropThumb(sha: sha, cornerRadius: Radius.card)
                        .frame(width: place.side, height: place.side)
                        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                            .strokeBorder(Palette.chalk.opacity(0.7), lineWidth: 1))
                        .overlay(alignment: .bottomLeading) {
                            if let name = leaLabel?.text, place.side >= HandoffPlacement.card { Text(name).cameraChip().padding(6) }
                        }
                }
                .buttonStyle(PressableCardStyle())
                .accessibilityLabel("Photo of \(leaLabel?.text ?? "the part")")
                .accessibilityHint("Opens it full screen")
                .offset(x: frame.minX, y: frame.minY)
                .transition(reduceMotion ? .opacity : .opacity.combined(with: .scale(scale: 0.96)))
                .id("\(sha)|\(place.corner)|\(Int(place.side))")
            }
        }
        .onGeometryChange(for: CGSize.self) { $0.size } action: { stageSize = $0 }
        .motion(Motion.standard, value: handoffPhoto)
        .animation(Motion.fade, value: probe.landedPoint)
        .fullScreenCover(item: $handoffZoom) { z in CropZoomView(shas: [z.sha], index: 0).environment(\.cropSource, cropSource) }
        .overlay(alignment: .top) {
            // What it is, as a compact chip in the top bar's row (between "Scans" and the round controls), never over
            // the model: brand and model only, one line. It steps out of the way while Lea is busy on the stage and
            // on the full-screen stage, and comes back when she is done.
            if let objectName, let chip = StageChip.text(objectName), !tour.active, stageLayout.detent != .expanded, !stageBusy {
                Text(chip).font(.subheadline.weight(.semibold)).foregroundStyle(Palette.chalk)
                    .lineLimit(1).truncationMode(.middle)
                    .padding(.horizontal, Spacing.m).frame(minHeight: 32)
                    .background { SmokeFill(shape: Capsule()) }
                    .dynamicTypeSize(...DynamicTypeSize.xxLarge)
                    .frame(height: 44)
                    .padding(.leading, 116).padding(.trailing, 112)   // clear of "‹ Scans" and the two round controls
                    .padding(.top, topInset + Spacing.xs)
                    .accessibilityLabel("This is \(objectName)")
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .offset(y: -12)))
            }
        }
        .motion(Motion.standard, value: stageBusy || stageLayout.detent == .expanded)
        .motion(Motion.standard, value: showsCaptions)
        .heightFromContainer(fallback: stageHeight) { [touring = tour.active, detent = stageLayout.detent] height in
            StageDetents.stageHeight(detent, container: height, touring: touring)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(stageLabel)
    }

    @ViewBuilder private var heroView: some View {
        let touring = tour.active
        let context = StageHeroContext(markers: pins, focus: touring ? nil : focus, select: { id in
            focus = id
            sheet = all.first { $0.id == id }
        }, ready: { modelReady = true }, probe: probe, tour: touring ? tour : nil, cue: touring ? nil : heroCue)
        Group {
            switch hero {
            case .custom(let make):
                make(context).padding(.top, topClear)
            case .automatic:
                if isPhotoHero, let sessionDir {
                    PhotoHero(stillsDir: sessionDir.appending(path: "stills"), numbered: numberedPhotos,
                              focus: touring ? tour.step?.focus : focus, onMarkTap: context.select)
                        .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                        .padding(.horizontal, Spacing.gutter)
                        .padding(.top, topClear)
                        .padding(.bottom, Spacing.l)
                } else {
                    ModelView(source: model, pins: pins, focus: focus, spin: true, showHint: false, stage: true,
                              onLoaded: { modelReady = true }, probe: probe, controls: !touring, tour: context.tour,
                              cue: context.cue, onPinTap: context.select)
                        // The object sits below the navigation bar drawn over the stage's top.
                        .padding(.top, topClear)
                }
            }
        }
        .padding(.bottom, Self.overlap + (touring ? Self.deckHeight : band))
    }

    /// The bottom of the stage the caption strip takes while it shows (measured; at least `captionBand`).
    private var band: CGFloat { showsCaptions ? captionHeight : 0 }

    /// The walkthrough's cards, header and controls (WalkthroughDeck), and the room the hero leaves for them.
    static let deckHeight: CGFloat = 44 + WalkthroughCard.height + 3 + 44 + 3 * Spacing.m + Spacing.m

    /// Plays the tour from its first card. `afterLea`: asked by voice, so it starts once she has said she will.
    /// A tour only when the report has problems or setup items to show (the owner: no guide through nothing).
    private var hasTourContent: Bool { !report.mostLikely.isEmpty || !report.otherIssues.isEmpty || !report.setup.isEmpty }

    private func startTour(afterLea: Bool = false) {
        guard hasTourContent else {
            LeaConversation.active?.leaSays("There's nothing to walk through: I didn't find any problems in this scan.")
            return
        }
        let photo = isPhotoHero, threeD = isCustomHero || !model.isNone
        let steps = Walkthrough.steps(report) { p in photo ? p.cropStill != nil : threeD && p.point != nil }
        sheet = nil
        showOthers = false
        focus = nil
        withAnimation(Motion.pick(Motion.standard, reduceMotion: reduceMotion)) { tour.start(steps, afterLea: afterLea) }
    }

    /// Lea showed something while answering: focus its marker (never opening its sheet: she is talking), ring a part
    /// that has no marker for a few seconds in a neutral tone, and move the hero's camera. A tour in progress ends
    /// (the person asked something else). Unknown parts are ignored.
    private func applyLea(_ cue: LeaStageCue?) {
        guard let cue else { return }
        let plan = LeaStagePlan.make(cue.command, pins: pins, located: report.located)
        guard plan != .none else { return }
        if tour.active { tour.stop() }
        answerStarted()   // her first move counts as the answer starting, as her first sentence does
        if let pin = plan.pin {
            // One of Lea's rings at a time: the previous one fades as this move starts (the hero fades leaving rings).
            leaPins = [pin]
            let id = pin.id
            Task {
                try? await Task.sleep(for: .seconds(Self.leaRingSeconds))
                leaPins.removeAll { $0.id == id }   // the camera stays where she put it
                if leaLabel?.id == id { leaLabel = nil }
            }
        }
        if let f = plan.focus { focus = f }
        switch cue.command {
        case .show(let id):
            let target = plan.pin?.id ?? id
            if let name = all.first(where: { $0.id == id })?.part { leaLabel = (target, name) } else { leaLabel = nil }
        case .point(let id, _, let label, _):
            leaLabel = label.trimmedOrNil.map { (LeaStagePlan.pinID(id), $0) }
        case .view(let side):
            if side == .reset { leaLabel = nil }
        case .zoom: break
        }
        switch cue.command {
        case .show(let id):
            if let p = all.first(where: { $0.id == id }) {
                let n = markerNumbers[id]
                captionPart = LeaCaptionStrip.Part(name: p.part, number: n, tone: n == nil ? nil : p.tone)
            }
        case .point(_, _, let label, _):
            captionPart = label.trimmedOrNil.map { LeaCaptionStrip.Part(name: $0) }
        case .view, .zoom: break
        }
        if let camera = plan.camera { heroCue = LeaStageCue(n: cue.n, command: camera) }
    }

    private var stageLabel: String {
        let list = report.mostLikely.enumerated().map { "\($0.offset + 1), \($0.element.part), \($0.element.issue)" }
        let setup = report.setup.enumerated().map { "\(StageMarks.setupNumber(report, $0.offset)), \($0.element.part), \($0.element.issue)" }
        return (isPhotoHero ? "Photo of the object" : "The scan in 3D") + (list.isEmpty ? "" : " with numbered problems: " + list.joined(separator: ". "))
            + (setup.isEmpty ? "" : ". Setup to check, not broken: " + setup.joined(separator: ". "))
    }

    // MARK: Sheet

    /// The sheet, in the order of DESIGN §7.5 (owner priority first): the verdict; safety (immediate: never waits for
    /// the landing); most likely problems; setup to check; the next step; what you suspected; in plain words; fix it;
    /// also worth a look; underneath; the full report. What wasn't checked is in the ⋯ menu, not here.
    private func sheetBody(_ proxy: ScrollViewProxy) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            Capsule().fill(Palette.secondary.opacity(0.35)).frame(width: 36, height: 5).frame(maxWidth: .infinity)
                .padding(.top, Spacing.s).padding(.bottom, -Spacing.m)
                .accessibilityHidden(true)
            StageVerdictHeader(report: report, symptom: symptom)
                .opacity(phase >= 2 ? 1 : 0)
                .offset(y: phase >= 2 || reduceMotion ? 0 : 24)
            // Safety is never delayed, faded or moved (M13).
            if !report.safety.isEmpty { StageSafetyCard(notes: report.safety) }
            VStack(alignment: .leading, spacing: Spacing.xl) {
                if !report.mostLikely.isEmpty {
                    VStack(alignment: .leading, spacing: Spacing.m) {
                        SectionHeader("Most likely problems", count: report.mostLikely.count)
                        ForEach(Array(report.mostLikely.enumerated()), id: \.element.id) { i, p in
                            problemRow(p, number: i + 1, proxy: proxy)
                                .opacity(phase >= 3 ? 1 : 0)
                                .animation(reduceMotion ? Motion.fade : Motion.standard.delay(Double(min(i, 2)) * 0.04), value: phase)
                        }
                    }
                }
                if !report.setup.isEmpty { setupToCheck(proxy) }
                Group {
                    // Not enough to call it: the next step is already the headline, so it isn't repeated. A request for
                    // more photos is not shown at all (the owner, 2026-10-25): Lea asks in conversation when she must.
                    if let next = report.nextStep, report.verdict != .insufficientEvidence, !ReportWords.isCaptureRequest(next) {
                        StageNextStep(step: next)
                    }
                    if let addMeterReading { meterReadingRow(addMeterReading) }
                    if let lead = report.userLead { UserLeadCard(lead: lead) }
                    if let summary = report.summary.map({ ReportWords.withoutCaveats(ReportWords.display($0)) })?.trimmedOrNil {
                        StagePlainWords(text: summary)
                    }
                    if fix != nil || fixSearching || fixUnavailable || askDesign != nil {
                        FixItCard(fix: fix, searching: fixSearching, unavailable: fixUnavailable, askDesign: askDesign).id("fix")
                    }
                    if !others.isEmpty { alsoWorthALook }
                    if !report.bottomPhotos.isEmpty { bottomPhotosSection }
                    if let openFull {
                        GroupedList {
                            Button(action: openFull) {
                                HStack {
                                    Label("Full report with photos", systemImage: "doc.richtext").foregroundStyle(Palette.accent)
                                    Spacer(minLength: 0)
                                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                                }
                                .font(.subheadline.weight(.semibold))
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(PressableCardStyle())
                        }
                    }
                }
            }
            .opacity(phase >= 3 ? 1 : 0)
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.bottom, Spacing.xxl)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.canvas, in: TopRoundedShape(radius: Radius.sheet))
    }

    @ViewBuilder private func problemRow(_ p: ReportModel.Problem, number: Int, proxy: ScrollViewProxy) -> some View {
        let located = isPhotoHero ? p.cropStill != nil : p.point != nil
        if located {
            Button {
                withAnimation(Motion.standard) { proxy.scrollTo("stage", anchor: .top) }
                focus = p.id
            } label: {
                StageProblemRow(problem: p, number: number, located: true, selected: focus == p.id)
            }
            .buttonStyle(PressableCardStyle())
        } else {
            NavigationLink(value: p.id) { StageProblemRow(problem: p, number: number, located: false) }
                .buttonStyle(PressableCardStyle())
        }
    }

    /// "Setup to check": every setup row with its fix, numbered like its marker, in the calm setup tone (not broken).
    private func setupToCheck(_ proxy: ScrollViewProxy) -> some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                SectionHeader("Setup to check", count: report.setup.count)
                Text(ReportWords.setupNote).font(.footnote).foregroundStyle(Palette.secondary)
            }
            ForEach(Array(report.setup.enumerated()), id: \.element.id) { i, p in
                let number = StageMarks.setupNumber(report, i)
                let located = isPhotoHero ? p.cropStill != nil : p.point != nil
                if located {
                    Button {
                        withAnimation(Motion.standard) { proxy.scrollTo("stage", anchor: .top) }
                        focus = p.id
                    } label: {
                        StageSetupRow(problem: p, number: number, located: true, selected: focus == p.id)
                    }
                    .buttonStyle(PressableCardStyle())
                } else {
                    NavigationLink(value: p.id) { StageSetupRow(problem: p, number: number, located: false) }
                        .buttonStyle(PressableCardStyle())
                }
            }
        }
        .id("setup")
    }

    /// "Underneath": the bottom scan's photos that could not be placed on the model, checked like the rest. A tap opens
    /// the photo full screen.
    private var bottomPhotosSection: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader("Underneath", count: report.bottomPhotos.count)
            ScrollView(.horizontal) {
                HStack(spacing: Spacing.s) {
                    ForEach(Array(report.bottomPhotos.enumerated()), id: \.element.id) { i, b in
                        Button { bottomZoom = BottomZoom(index: i) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                CropThumb(sha: b.crop, cornerRadius: Radius.small).frame(width: 96, height: 96)
                                Text(b.part).font(.caption).foregroundStyle(Palette.secondary).lineLimit(1).frame(width: 96, alignment: .leading)
                            }
                        }
                        .buttonStyle(PressableCardStyle())
                        .accessibilityLabel("Photo of the underside: \(b.part)")
                        .accessibilityHint("Opens the photo full screen")
                    }
                }
            }
            .scrollIndicators(.hidden)
        }
        .fullScreenCover(item: $bottomZoom) { z in
            CropZoomView(shas: report.bottomPhotos.map(\.crop), index: z.index).environment(\.cropSource, cropSource)
        }
    }

    /// The other issues and cosmetic findings: one grouped row each (crop, part, issue in its tone), the first three,
    /// then "Show all".
    private var alsoWorthALook: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader("Also worth a look", count: others.count)
            GroupedList {
                ForEach(others.prefix(3)) { p in
                    NavigationLink(value: p.id) {
                        HStack(spacing: Spacing.m) {
                            CropThumb(sha: p.crops.first, cornerRadius: Radius.small).frame(width: 48, height: 48)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.part).font(.subheadline.weight(.semibold)).foregroundStyle(Palette.ink)
                                Text(p.issue).font(.footnote.weight(.semibold)).foregroundStyle(p.tone.text)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                        }
                        .padding(.vertical, Spacing.m)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableCardStyle())
                }
                if others.count > 3 {
                    Button("Show all \(others.count)") { showOthers = true }
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                }
            }
        }
    }

    // MARK: Landing

    private func land() async {
        guard fresh, !reduceMotion else {
            withAnimation(Motion.fade) { phase = 3 }
            if fresh { announce() }
            return
        }
        let before = heard
        phase = 0
        // Wait for the scan to be on the stage (the cloud can take a moment to read), then land on it.
        let start = Date()
        while !modelReady, !isPhotoHero, isCustomHero || !model.isNone, Date().timeIntervalSince(start) < Self.maxWait {
            try? await Task.sleep(for: .milliseconds(50))
        }
        withAnimation(Motion.reveal) { phase = 1 }
        // The markers pop in from 0.15 s, Motion.stagger apart (ModelView); the sheet follows the last one.
        try? await Task.sleep(for: .seconds(0.15 + Motion.stagger * Double(max(0, findingPins.count - 1)) + 0.1))
        // Land the verdict on the frame Lea starts speaking (report → first sound measured 118–305 ms); never wait
        // past `voiceWait` (muted, offline, or this revision already spoken).
        let waitFrom = Date()
        while heard == before, !LeaVoice.shared.muted, Date().timeIntervalSince(waitFrom) < Self.voiceWait {
            try? await Task.sleep(for: .milliseconds(16))
        }
        withAnimation(Motion.rise) { phase = 2 }
        announce()
        try? await Task.sleep(for: .seconds(0.45))
        withAnimation(Motion.standard) { phase = 3 }
    }

    /// VoiceOver hears the headline once when a report lands, unless Lea is already saying it.
    private func announce() {
        guard voiceOver, LeaVoice.shared.muted else { return }
        AccessibilityNotification.Announcement("\(ReportWords.eyebrow(report)). \(ReportWords.headline(report))").post()
    }
}

// MARK: - Marks on the stage

/// What the stage marks, numbered like the sheet's rows: the most likely problems (1…3, their severity tone), then the
/// setup rows (report `setup`: a battery missing, a cable unplugged) numbered after them in the calm setup tone. Setup
/// rows get a marker and "Show on object" like a problem, but are never counted as problems (ReportWords counts
/// `mostLikely` only).
enum StageMarks {
    /// The number on a setup row and on its marker: after the most likely problems.
    static func setupNumber(_ r: ReportModel, _ index: Int) -> Int { r.mostLikely.count + index + 1 }

    static func pins(_ r: ReportModel) -> [ModelPin] {
        (r.mostLikely + r.setup).enumerated().compactMap { i, p in
            p.point.map { ModelPin(id: p.id, number: i + 1, point: $0, tone: p.tone, area: p.extent) }
        }
    }

    static func photoMarks(_ r: ReportModel) -> [PhotoHero.Mark] {
        (r.mostLikely + r.setup).enumerated().compactMap { i, p in
            guard let still = p.cropStill else { return nil }
            return PhotoHero.Mark(id: p.id, number: i + 1, still: still, box: p.cropBox, tone: p.tone)
        }
    }
}

// MARK: - Smaller sheet pieces

/// The next step: the step itself is the section title ("Take a closer photo"), then the instruction and the reason.
/// No "Next step" above it (an eyebrow) and no button: its action is in the pinned bar.
struct StageNextStep: View {
    let step: ReportModel.NextStep
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            SectionHeader(Walkthrough.specificTitle(step) ?? step.title)
            Text(ReportWords.display(step.instruction)).font(.body).foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let reason = step.reason?.trimmedOrNil {
                Text(ReportWords.display(reason)).font(.subheadline).foregroundStyle(Palette.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(step.safetyNotes.indices, id: \.self) { i in
                Label(step.safetyNotes[i], systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline).foregroundStyle(Palette.caution)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// The plain-words summary, unboxed, cut to five lines with "Read more".
struct StagePlainWords: View {
    let text: String
    @State private var open = false
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            SectionHeader("In plain words")
            Text(text).font(.body).foregroundStyle(Palette.ink).lineSpacing(3).lineLimit(open ? nil : 5).textSelection(.enabled)
            if text.count > 240 {
                Button(open ? "Show less" : "Read more") { withAnimation(Motion.standard) { open.toggle() } }
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
                    .frame(minHeight: 44)
            }
        }
    }
}

// MARK: - Waiting

/// While the Mac writes the report: the person's own scan on the stage (turning slowly), one line of progress, and
/// a count of what has been found so far. Replaces the orb + checklist; errors still use ReportProgressView.
struct ReportStageWaiting: View {
    var model: ModelSource
    let progress: PipelineProgress
    let uploaded: Int
    let pending: Int
    /// Findings already seen live (LiveEvents), when known.
    var found: Int?

    var body: some View {
        ZStack {
            StageBackground().ignoresSafeArea()
            if !model.isNone {
                // The same framing as the report's stage (a wide frame, not the tall screen), a little above centre.
                ModelView(source: model, pins: [], spin: true, showHint: false, stage: true, controls: false) { _ in }
                    .frame(height: 380)
                    .frame(maxHeight: .infinity, alignment: .center)
                    .padding(.bottom, 120)
                    .allowsHitTesting(false)
            }
            VStack(spacing: Spacing.m) {
                Spacer()
                StageStatus(text: line)
                if let found, found > 0 {
                    Text(found == 1 ? "1 thing to look at so far" : "\(found) things to look at so far")
                        .font(.subheadline).foregroundStyle(Palette.chalk.opacity(0.75))
                        .contentTransition(.numericText())
                }
                if let fraction { StageProgressLine(fraction: fraction).padding(.top, Spacing.xs) }
            }
            .padding(.bottom, Spacing.xxl + Spacing.l)
            .motion(Motion.standard, value: line)
        }
        .toolbarBackground(.hidden, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
    }

    private var line: String {
        switch progress.step {
        case .upload:
            uploaded + pending > 0 ? "Sending photo \(min(uploaded + 1, uploaded + pending)) of \(uploaded + pending)" : "Sending photos"
        case .inspect:
            progress.stillsTotal > 0 ? "Looking at photo \(min(progress.stillsDone + 1, progress.stillsTotal)) of \(progress.stillsTotal)"
                : "Looking at each photo"
        case .weigh: "Weighing what it saw"
        case .write: "Writing it up"
        }
    }

    private var fraction: Double? {
        switch progress.step {
        case .upload: uploaded + pending > 0 ? Double(uploaded) / Double(uploaded + pending) : nil
        case .inspect: progress.stillsTotal > 0 ? Double(progress.stillsDone) / Double(progress.stillsTotal) : nil
        default: nil
        }
    }
}

private struct HandoffZoom: Identifiable {
    let sha: String
    var id: String { sha }
}

/// Which bottom photo is open full screen.
private struct BottomZoom: Identifiable {
    let index: Int
    var id: Int { index }
}


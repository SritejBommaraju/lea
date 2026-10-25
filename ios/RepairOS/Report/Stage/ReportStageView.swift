import SwiftUI

/// The redesigned report (RedesignFlags.reportStage): the scan full-bleed on the dark stage, with the report sheet
/// rising over it. Same inputs as `ReportContent`, so swapping it in is a few lines in ReportView.
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
    /// The hero on the stage. `.automatic` follows `model` (3D view or the photo hero); a 3D recreation plugs in
    /// with `.custom` (see `StageHeroContext`) without touching anything else here.
    var hero: StageHero = .automatic

    @Environment(\.continueScan) private var continueScan
    @Environment(\.cropSource) private var cropSource
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOver
    @State var focus: String?
    @State private var sheet: ReportModel.Problem?
    @State private var showOnObject = false
    @State private var showOthers = false
    /// 0 nothing, 1 stage, 2 sheet (verdict), 3 rows.
    @State private var phase = 0
    /// Circle to inspect: drawing mode, the circled spot waiting for a choice, and the session's circled targets.
    @State private var circling = false
    @State private var circled: InspectTarget?
    @State private var circleMiss = false
    @State private var targets: [InspectTarget] = []
    @State private var probe = StageProbe()
    /// Counts Lea's first sounds seen (from `voiceStarted`), read live by the landing task.
    @State private var heard = 0
    /// The 3D scan is on screen: the landing waits for it (at most `maxWait`), so markers and verdict arrive on it.
    @State private var modelReady = false
    private static let maxWait: Double = 2.5
    /// Longest the verdict waits for Lea's first sound after the markers have landed.
    private static let voiceWait: Double = 0.35

    // MARK: Derived

    private var pins: [ModelPin] {
        StageMarks.pins(report) + targets.filter { !$0.checked }.compactMap { t in
            t.simd.map { ModelPin(id: "target:" + t.id, number: nil, point: $0, tone: .accent, ringRadius: t.radius) }
        }
    }

    /// The numbered findings and setup rows only (the circled rings are not problems).
    private var findingPins: [ModelPin] { pins.filter { $0.ringRadius == nil } }

    private var numberedPhotos: [PhotoHero.Mark] { StageMarks.photoMarks(report) }

    /// Lower-ranked and cosmetic findings; setup rows have their own section ("Setup to check").
    private var others: [ReportModel.Problem] { report.otherIssues + report.cosmetic }
    private var all: [ReportModel.Problem] { report.mostLikely + report.otherIssues + report.setup + report.cosmetic }
    private var isPhotoHero: Bool { if case .photos = model { true } else { false } }
    private var canShowOnObject: Bool { space != nil && !findingPins.isEmpty }
    private var isCustomHero: Bool { if case .custom = hero { true } else { false } }
    private var canCircle: Bool { (isCustomHero || (!isPhotoHero && !model.isNone)) && sessionDir != nil }
    private var landingKey: String { "\(report.verdict)|\(report.mostLikely.map(\.id))" }

    // MARK: Body

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                    stage.id("stage")
                    sheetBody(proxy)
                        .padding(.top, -Self.overlap)
                        .opacity(phase >= 2 ? 1 : 0)
                        .offset(y: phase >= 2 || reduceMotion ? 0 : 24)
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            // Pulling down past the top shows more stage, never a strip of canvas.
            .background(alignment: .top) { StageBackground().frame(height: 700) }
            .background(Palette.canvas)
            .ignoresSafeArea(edges: .top)
            .navigationDestination(for: String.self) { id in
                if let p = all.first(where: { $0.id == id }) { ProblemDetailView(problem: p) }
            }
        }
        .toolbarBackground(.hidden, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .sensoryFeedback(trigger: phase) { _, new in
            guard new == 2, fresh else { return nil }
            return report.verdict == .potentialIssues ? Haptic.verdictProblems : (report.verdict == .looksGood ? Haptic.verdictClear : nil)
        }
        .sensoryFeedback(Haptic.select, trigger: focus) { _, new in new != nil }
        .task(id: landingKey) { await land() }
        .onChange(of: voiceStarted) { heard += 1 }
        .fullScreenCover(isPresented: $showOnObject) {
            if let space {
                ObjectMarkersView(space: space, markers: findingPins.map { ObjectMarker(id: $0.id, number: $0.number, point: $0.point, tone: $0.tone) },
                                  problems: all, others: others, show3D: { showOnObject = false }, close: { showOnObject = false })
                    .environment(\.cropSource, cropSource)
            }
        }
        .sheet(isPresented: $showOthers) {
            OtherFindingsSheet(problems: others) { p in
                showOthers = false
                Task { try? await Task.sleep(for: .milliseconds(350)); sheet = p }
            }
            .environment(\.cropSource, cropSource)
        }
        .sheet(item: $sheet) { p in
            NavigationStack {
                ProblemDetailView(problem: p)
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { sheet = nil }.fontWeight(.semibold) } }
            }
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .sheet(item: $circled) { target in
            LookCloserSheet(canScan: continueScan != nil) {
                keep(target)
                circled = nil
                continueScan?()
            } keep: {
                keep(target)
                circled = nil
            } cancel: { circled = nil }
            .presentationDetents([.height(260)])
        }
        .task(id: sessionDir) { if let sessionDir { targets = InspectTargets.load(sessionDir) } }
        .sensoryFeedback(Haptic.select, trigger: circled?.id) { _, new in new != nil }
    }

    /// Saves a circled spot next to the session; Continue scanning aims its "Move closer here" marker at it.
    private func keep(_ target: InspectTarget) {
        guard let sessionDir else { return }
        InspectTargets.add(target, sessionDir)
        withAnimation(Motion.pop) { targets = InspectTargets.load(sessionDir) }
    }

    /// How far the sheet rides up over the stage.
    static let overlap: CGFloat = 28
    /// Status bar + navigation bar over the stage: the 3D view starts below them so the object is never under them.
    static let topClear: CGFloat = 96

    // MARK: Stage

    private var stage: some View {
        ZStack(alignment: .top) {
            StageBackground()
            heroView
            .padding(.bottom, Self.overlap)
            .overlay(alignment: .bottomLeading) {
                HStack(spacing: Spacing.s) {
                    if canShowOnObject {
                        Button { showOnObject = true } label: { Label("Show on object", systemImage: "arkit") }
                            .buttonStyle(StageCapsuleButtonStyle())
                            .accessibilityHint("Places the numbered markers on the real object with the camera")
                    }
                    if !others.isEmpty {
                        Button { showOthers = true } label: {
                            Text("+\(others.count)").font(.system(.subheadline, design: .rounded, weight: .bold)).monospacedDigit()
                        }
                        .buttonStyle(StageCapsuleButtonStyle())
                        .accessibilityLabel("\(others.count) more findings")
                    }
                }
                .padding(.leading, Spacing.l)
                .padding(.bottom, Self.overlap + Spacing.m)
            }
            .overlay {
                if circling {
                    CircleDrawLayer { center, radius in circle(center, radius) }
                        .padding(.top, Self.topClear).padding(.bottom, Self.overlap)
                }
            }
            .overlay(alignment: .top) {
                if circling || circleMiss {
                    Text(circleMiss ? "No part of the scan there. Try again." : "Circle the spot to look closer at")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold)).foregroundStyle(.white)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(Color.black.opacity(0.55), in: Capsule())
                        .padding(.top, Self.topClear + Spacing.s)
                        .transition(.opacity)
                        .allowsHitTesting(false)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if canCircle {
                    // Circle to inspect, beside the recenter control (ModelView draws that one at the corner).
                    Button { withAnimation(Motion.fade) { circling.toggle(); circleMiss = false } } label: {
                        Image(systemName: circling ? Symbol.close : "lasso")
                    }
                    .buttonStyle(StageIconButtonStyle())
                    .padding(.trailing, Spacing.m + 44 + Spacing.s)
                    .padding(.bottom, Self.overlap + Spacing.m)
                    .accessibilityLabel(circling ? "Stop circling" : "Circle a spot to look closer at")
                }
            }
        }
        .containerRelativeFrame(.vertical) { height, _ in max(380, height * 0.54) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(stageLabel)
    }

    @ViewBuilder private var heroView: some View {
        let context = StageHeroContext(markers: pins, focus: focus, select: { id in
            focus = id
            sheet = all.first { $0.id == id }
        }, ready: { modelReady = true }, probe: probe)
        Group {
            switch hero {
            case .custom(let make):
                make(context).padding(.top, Self.topClear)
            case .automatic:
                if isPhotoHero, let sessionDir {
                    PhotoHero(stillsDir: sessionDir.appending(path: "stills"), numbered: numberedPhotos, focus: focus,
                              onMarkTap: context.select)
                        .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
                        .padding(.horizontal, Spacing.gutter)
                        .padding(.top, Self.topClear)
                        .padding(.bottom, Spacing.l)
                } else {
                    ModelView(source: model, pins: pins, focus: focus, spin: true, showHint: false, stage: true,
                              onLoaded: { modelReady = true }, probe: probe, onPinTap: context.select)
                        // The object sits below the navigation bar drawn over the stage's top.
                        .padding(.top, Self.topClear)
                }
            }
        }
        .padding(.bottom, Self.overlap)
    }

    /// A circle drawn on the stage: find the spot on the scan, then ask what to do with it.
    private func circle(_ center: CGPoint, _ radius: CGFloat) {
        // The draw layer sits below `topClear`, the 3D view too, so the two share coordinates.
        guard let hit = probe.resolve?(center, radius) else {
            withAnimation(Motion.fade) { circleMiss = true }
            return
        }
        circleMiss = false
        circling = false
        circled = InspectTarget(point: [hit.point.x, hit.point.y, hit.point.z], radius: hit.radius)
    }

    private var stageLabel: String {
        let list = report.mostLikely.enumerated().map { "\($0.offset + 1), \($0.element.part), \($0.element.issue)" }
        let setup = report.setup.enumerated().map { "\(StageMarks.setupNumber(report, $0.offset)), \($0.element.part), \($0.element.issue)" }
        return (isPhotoHero ? "Photo of the object" : "The scan in 3D") + (list.isEmpty ? "" : " with numbered problems: " + list.joined(separator: ". "))
            + (setup.isEmpty ? "" : ". Setup to check, not broken: " + setup.joined(separator: ". "))
    }

    // MARK: Sheet

    private func sheetBody(_ proxy: ScrollViewProxy) -> some View {
        VStack(alignment: .leading, spacing: Spacing.xl) {
            Capsule().fill(.tertiary).frame(width: 36, height: 5).frame(maxWidth: .infinity)
                .padding(.top, Spacing.s).padding(.bottom, -Spacing.m)
                .accessibilityHidden(true)
            StageVerdictHeader(report: report, symptom: symptom)
            if report.verdict == .noProblemsSeen, continueScan != nil || canCircle {
                // Good news with a caveat: offer the two ways to check what could not be seen.
                HStack(spacing: Spacing.s) {
                    if let continueScan {
                        Button { continueScan() } label: { Label("Continue scanning", systemImage: Symbol.scan) }
                            .buttonStyle(.primary(.quiet))
                    }
                    if canCircle {
                        Button {
                            withAnimation(Motion.standard) { proxy.scrollTo("stage", anchor: .top) }
                            withAnimation(Motion.fade) { circling = true }
                        } label: { Label("Look closer", systemImage: "lasso") }
                            .buttonStyle(.primary(.quiet))
                    }
                }
            }
            if !report.safety.isEmpty { StageSafetyLine(notes: report.safety) }
            if let lead = report.userLead { UserLeadCard(lead: lead) }
            if !report.mostLikely.isEmpty {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    ForEach(Array(report.mostLikely.enumerated()), id: \.element.id) { i, p in
                        problemRow(p, number: i + 1, proxy: proxy)
                            .opacity(phase >= 3 ? 1 : 0)
                            .animation(reduceMotion ? Motion.fade : Motion.standard.delay(Double(min(i, 2)) * 0.04), value: phase)
                    }
                }
            }
            if !report.setup.isEmpty {
                setupToCheck(proxy).opacity(phase >= 3 ? 1 : 0)
            }
            Group {
                if fix != nil || fixSearching || fixUnavailable {
                    FixItCard(fix: fix, searching: fixSearching, unavailable: fixUnavailable).id("fix")
                }
                // Not enough to call it: the next step is already the headline, so no card repeats it.
                if let next = report.nextStep, report.verdict != .insufficientEvidence { StageNextStep(step: next) }
                if let summary = report.summary?.trimmedOrNil { StagePlainWords(text: ReportWords.sanitize(summary)) }
                if !others.isEmpty { alsoWorthALook }
                CouldNotCheckCard(notInspected: report.notInspected, unclear: report.insufficient,
                                  continueScan: continueScan.map { action -> () -> Void in { action() } })
                    .id("gaps")
                if !report.limits.isEmpty {
                    Label { Text(report.limits.map(ReportWords.sanitize).joined(separator: " ")) } icon: { Image(systemName: "info.circle") }
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if let openFull {
                    Button(action: openFull) { Label("Full report with photos", systemImage: "doc.richtext") }
                        .buttonStyle(.primary(.quiet))
                }
            }
            .opacity(phase >= 3 ? 1 : 0)
        }
        .padding(.horizontal, Spacing.gutter)
        .padding(.bottom, Spacing.xxl)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.canvas, in: UnevenRoundedRectangle(topLeadingRadius: Self.overlap, topTrailingRadius: Self.overlap,
                                                               style: .continuous))
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
                Text(ReportWords.setupNote).font(.footnote).foregroundStyle(.secondary)
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

    private var alsoWorthALook: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader("Also worth a look", count: others.count)
            VStack(spacing: 0) {
                ForEach(Array(others.prefix(3).enumerated()), id: \.element.id) { i, p in
                    if i > 0 { Divider().padding(.leading, Spacing.l) }
                    NavigationLink(value: p.id) {
                        HStack(spacing: Spacing.m) {
                            CropThumb(sha: p.crops.first, cornerRadius: Radius.small).frame(width: 48, height: 48)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(p.part).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                                Text(p.issue).font(.footnote).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        }
                        .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(PressableCardStyle())
                }
                if others.count > 3 {
                    Divider()
                    Button("Show all \(others.count)") { showOthers = true }
                        .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(Spacing.l)
                }
            }
            .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: 0.5))
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
            p.point.map { ModelPin(id: p.id, number: i + 1, point: $0, tone: p.tone) }
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

/// The next step, compact: icon, title, instruction.
struct StageNextStep: View {
    let step: ReportModel.NextStep
    var body: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            IconBadge(symbol: step.symbol, tone: .accent)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text("Next step").font(.pillLabel).foregroundStyle(Palette.accent)
                Text(step.title).font(.headline)
                Text(ReportWords.sanitize(step.instruction)).font(.subheadline).fixedSize(horizontal: false, vertical: true)
            }
        }
        .card(tone: .accent)
        .accessibilityElement(children: .combine)
    }
}

/// The plain-words summary, cut to five lines with "Read more".
struct StagePlainWords: View {
    let text: String
    @State private var open = false
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text("In plain words").font(.headline)
            Text(text).font(.body).lineSpacing(3).lineLimit(open ? nil : 5).textSelection(.enabled)
            if text.count > 240 {
                Button(open ? "Show less" : "Read more") { withAnimation(Motion.standard) { open.toggle() } }
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
            }
        }
        .card()
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
                        .font(.subheadline).foregroundStyle(.white.opacity(0.65))
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

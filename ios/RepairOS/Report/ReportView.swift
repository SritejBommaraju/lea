import SwiftUI
import WebKit

/// Upload and analysis progress, then the report. Leads with the verdict, the safety notes and the "Most likely
/// problems" top 3 with their crops; then the next step, the plain-words summary, cosmetic and lower-ranked
/// issues, and what was not inspected or could not be judged (always with the reason). Confidence is a word,
/// never a number (A5).
struct ReportView: View {
    @Environment(AppState.self) private var app
    let record: SessionRecord
    /// nil for an offline replay (Demo replay): only what is on the phone is shown.
    let upload: UploadQueue?
    let close: () -> Void
    /// Demo replay: never touches the network; the cached report, the scan's own files and saved crops only.
    var offline = false

    @State private var report: ReportModel?
    /// The report on screen was just fetched (play the landing moment); false for one read from the phone.
    @State private var landedFresh = false
    /// Bumped on the frame Lea's first sound plays for a landed report, so the verdict lands with her voice.
    @State private var voiceStarted = 0
    /// The revision on screen and whether a newer one is being built (the "Updating…" chip).
    @State private var showingRev: Int?
    @State private var updating = false
    @State private var progress = PipelineProgress()
    @State private var error: String?
    @State private var showFull = false
    @State private var pendingDelete: SessionRecord?
    @State private var model: ModelSource = .none
    /// The gateway mesh revision cached beside the scan (nil: none fetched in this view yet).
    @State private var meshRev: Int?
    /// The hero the pipeline reports (points, mesh, photos), when it has chosen.
    @State private var pipelineHero: String?
    /// The backend is still looking for the top finding's part ("Fix it"): until `fix_ready`, or FixSearch.giveUpS.
    @State private var fixSearching = false
    /// The parts search hit the spend cap (pipeline `fix_status` or live `fix_ready`): "not available right now".
    @State private var fixLimited = false
    @Environment(\.continueScan) private var continueScan
    @Environment(\.liveEvents) private var live

    private var gateway: GatewayClient? { offline ? nil : app.gateway }

    var body: some View {
        NavigationStack {
            Group {
                if let report, RedesignFlags.reportStage {
                    ReportStageView(report: report, symptom: record.symptom, model: model, sessionDir: app.storage.dir(record),
                                    space: ObjectSpace.load(sessionDir: app.storage.dir(record), scanPackage: app.storage.scanPackageURL(record)),
                                    openFull: gateway == nil ? nil : { showFull = true },
                                    fix: live?.fix ?? report.fix,
                                    fixSearching: fixSearching && live?.fix == nil && live?.fixEnded == nil && report.fix == nil,
                                    fixUnavailable: (fixLimited || live?.fixEnded == "limited") && live?.fix == nil && report.fix == nil,
                                    fresh: landedFresh, voiceStarted: voiceStarted)
                        .transition(.opacity)
                        .overlay(alignment: .top) {
                            if updating {
                                Label("Updating…", systemImage: "arrow.triangle.2.circlepath")
                                    .font(.footnote.weight(.semibold)).foregroundStyle(.white)
                                    .padding(.horizontal, 12).padding(.vertical, 6)
                                    .background(Color.black.opacity(0.55), in: Capsule())
                                    .padding(.top, 52)
                                    .transition(.opacity)
                            }
                        }
                } else if let report {
                    ReportContent(report: report, symptom: record.symptom, model: model, sessionDir: app.storage.dir(record),
                                  space: ObjectSpace.load(sessionDir: app.storage.dir(record), scanPackage: app.storage.scanPackageURL(record)),
                                  openFull: gateway == nil ? nil : { showFull = true },
                                  fix: live?.fix ?? report.fix,
                                  fixSearching: fixSearching && live?.fix == nil && live?.fixEnded == nil && report.fix == nil,
                                  fixUnavailable: (fixLimited || live?.fixEnded == "limited") && live?.fix == nil && report.fix == nil)
                        .transition(.opacity)
                        .overlay(alignment: .top) {
                            if updating {
                                Label("Updating…", systemImage: "arrow.triangle.2.circlepath")
                                    .font(.footnote.weight(.semibold))
                                    .padding(.horizontal, 12).padding(.vertical, 6)
                                    .background(.thinMaterial, in: Capsule())
                                    .padding(.top, 6)
                                    .transition(.opacity)
                            }
                        }
                } else if RedesignFlags.reportStage, error == nil {
                    // While the Mac works: the person's own scan on the dark stage, one line of progress.
                    ReportStageWaiting(model: model, progress: progress, uploaded: upload?.uploadedFiles ?? 0,
                                       pending: upload?.pending.count ?? 0, found: live?.areasChecked)
                } else {
                    ReportProgressView(progress: progress, uploaded: upload?.uploadedFiles ?? 0,
                                       pending: upload?.pending.count ?? 0, error: error, retry: retry)
                }
            }
            .motion(Motion.standard, value: report == nil)
            .safeAreaInset(edge: .bottom) {
                // Voice/: hold to ask Lea about this scan (needs the gateway: the answer is grounded in its report).
                if report != nil, let gateway, app.isPaired {
                    ConversationPanel(scanID: record.scanID, gateway: gateway, diag: Diag.session(app.storage.dir(record)),
                                      sessionID: { [upload] in upload?.state.gatewaySessionID }) { kind in
                        if kind == "continue_scan" { continueScan?() }
                    }
                }
            }
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle(report == nil || RedesignFlags.reportStage ? "" : "Report")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done", action: close).fontWeight(.semibold) }
                ToolbarItem(placement: .primaryAction) { VoiceMuteToolbarButton() }
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        if report != nil, let continueScan {
                            Button("Continue scanning", systemImage: Symbol.scan) { continueScan() }
                        }
                        if report != nil, gateway != nil {
                            Button("Full report with photos", systemImage: "doc.richtext") { showFull = true }
                        }
                        Button("Delete scan", systemImage: "trash", role: .destructive) { pendingDelete = record }
                            .disabled(offline || !ScanLibrary.canDelete(record, app: app))
                    } label: { Image(systemName: "ellipsis") }
                    .accessibilityLabel("More")
                }
            }
            .confirmDeleteScan($pendingDelete, deleted: close)
            .sheet(isPresented: $showFull) {
                if let gateway {
                    NavigationStack {
                        WebView(request: gateway.reportHTMLRequest(scanID: record.scanID))
                            .ignoresSafeArea(edges: .bottom)
                            .navigationTitle("Full report").navigationBarTitleDisplayMode(.inline)
                            .toolbar { Button("Close") { showFull = false }.fontWeight(.semibold) }
                    }
                }
            }
            .task { await run() }
            .task(id: report == nil) { await fetchMesh(rev: nil) }
            .task(id: live?.meshRev) { if let rev = live?.meshRev { await fetchMesh(rev: rev) } }
            .task(id: heroChoice) { await fetchMesh(rev: nil) }
            .task(id: report?.cropSHAs.count) { await saveCrops() }
            .task(id: fixSearching) {
                guard fixSearching else { return }
                try? await Task.sleep(for: .seconds(FixSearch.giveUpS))
                withAnimation(Motion.standard) { fixSearching = false }
            }
            .onChange(of: live?.fix) { _, fix in if let fix { keepFix(fix) } }
        }
        .environment(\.cropSource, CropSource.saved(in: CropSource.dir(app.storage.dir(record)), fallback: gateway.map(CropSource.gateway)))
    }

    /// A report arrived: Lea says the verdict, the top problem and the first safety note (Voice/ReportSpeech),
    /// once per revision; the time to the first sound goes to diag.jsonl as `voice_report`.
    private func speakLanded(_ json: [String: Any], rev: Int, force: Bool = false) {
        let diag = Diag.session(app.storage.dir(record))
        let started = $voiceStarted
        let caveat = (json["verdict_reason"] as? [String: Any])?["caveat"] as? String
        LeaVoice.shared.reportLanded(ReportModel(json: json), key: "\(record.scanID)#\(rev)", force: force, caveat: caveat) { ms, source in
            started.wrappedValue += 1   // the stage lands its verdict on this frame (ReportStageView.land)
            diag.log("voice_report", ["landed_to_audio_ms": ms, "source": source, "rev": rev])
        }
    }

    private func retry() {
        error = nil
        upload?.retry()
        Task { await run() }
    }

    /// The gateway's mesh of the scan (GET /v1/scans/{id}/mesh), once, cached beside the scan; until it exists the
    /// local LiDAR cloud is shown.
    /// Every crop of the report, saved beside the session (once each), so it reopens offline (and Demo replay works).
    private func saveCrops() async {
        guard let shas = report?.cropSHAs, !shas.isEmpty, let gateway else { return }
        let source = CropSource.saved(in: CropSource.dir(app.storage.dir(record)), fallback: .gateway(gateway))
        for sha in shas {
            if Task.isCancelled { return }
            _ = await source.fetch(sha)
        }
    }

    /// The backend's hero choice: the live `mesh_ready`, else the pipeline's, else the cached report's.
    private var heroChoice: String? { live?.hero ?? pipelineHero ?? report?.hero }

    /// Follows the backend's hero choice: fetches its cleaned cloud (`points`) or its mesh (`mesh`), cached beside the
    /// scan; `photos` needs nothing. `rev`: a newer revision was announced (live `mesh_ready` or the pipeline's
    /// `mesh_rev`): fetch again even when cached. nil: fetch only what is missing. Until the backend has chosen, the
    /// phone's own cloud is the placeholder.
    private func fetchMesh(rev: Int?) async {
        let dir = app.storage.scanDir(record)
        let hero = heroChoice
        model = ModelSource.find(in: dir, hero: hero)
        guard report != nil, let gateway, !offline, hero != "photos" else { return }
        let wantCloud = hero == "points"
        let name = wantCloud ? ModelSource.backendCloudName : ModelSource.backendMeshName
        let cached = FileManager.default.fileExists(atPath: dir.appending(path: name).path)
        if let rev { guard rev != meshRev || !cached else { return } } else if cached { return }
        let data = try? await (wantCloud ? gateway.cloud(scanID: record.scanID) : gateway.mesh(scanID: record.scanID))
        guard let data, data.starts(with: Data("ply".utf8)) else { return }
        meshRev = rev ?? meshRev
        guard (try? data.write(to: dir.appending(path: name), options: .atomic)) != nil else { return }
        Diag.session(app.storage.dir(record)).log(wantCloud ? "cloud_fetched" : "mesh_fetched", ["kb": data.count / 1024, "hero": hero ?? ""])
        withAnimation(Motion.standard) { model = ModelSource.find(in: dir, hero: hero) }
    }

    /// A fix that arrived live goes into the cached report too, so it is there when the report is opened again.
    private func keepFix(_ fix: FixIt) {
        withAnimation(Motion.standard) { fixSearching = false }
        guard var cached = CachedReport.load(record, storage: app.storage) else { return }
        cached["fix"] = fix.json
        CachedReport.save(cached, record: record, storage: app.storage)
    }

    private func run() async {
        guard report == nil else { return }
        if offline {
            if let cached = CachedReport.load(record, storage: app.storage) {
                report = ReportModel(json: cached)
            } else {
                error = "This scan has no saved report on the phone."
            }
            return
        }
        // Whatever report the phone has goes on screen at once: a continued scan keeps it while the new one is built.
        if let cached = CachedReport.load(record, storage: app.storage) {
            report = ReportModel(json: cached)
            showingRev = cached[Self.revKey] as? Int ?? 0
            // Developer hook (`--voice-speak-report`): say a saved report on opening, to time the voice.
            if CommandLine.arguments.contains("--voice-speak-report") {
                speakLanded(cached, rev: showingRev ?? 0, force: true)
            }
            if record.status == .reported { return }
            updating = true
        }
        do {
            guard let gateway, let upload else { throw GatewayError.message("Not connected to the lea server. Connect in Settings, then try again.") }
            progress.step = .upload
            try await upload.complete()
            progress.step = .inspect
            while true {
                let p = try await gateway.pipeline(scanID: record.scanID)
                progress.update(p)
                if let h = p["hero"] as? String, h != pipelineHero { pipelineHero = h }
                let searching = (p["fix_status"] as? String) == "searching"
                if searching != fixSearching { withAnimation(Motion.standard) { fixSearching = searching } }
                fixLimited = (p["fix_status"] as? String) == "limited"
                if report != nil, let rev = p["mesh_rev"] as? Int, rev != meshRev { await fetchMesh(rev: rev) }
                let action = ReportPoll.decide(showingRev: showingRev, status: p["status"] as? String,
                                               reportRev: p["report_rev"] as? Int, rebuilding: p["rebuilding"] as? Bool ?? false,
                                               error: (p["error"] as? String) ?? (p["error"].map { "\($0)" }))
                switch action {
                case .fetch:
                    do {
                        let (json, rev) = try await gateway.reportWithRev(scanID: record.scanID)
                        var stored = json
                        let shown = rev ?? (p["report_rev"] as? Int) ?? (showingRev ?? 0) + 1
                        stored[Self.revKey] = shown
                        CachedReport.save(stored, record: record, storage: app.storage)
                        landedFresh = true
                        withAnimation(Motion.standard) { report = ReportModel(json: json) }
                        speakLanded(json, rev: shown)
                        showingRev = shown
                        updating = p["rebuilding"] as? Bool ?? false
                        if !updating { markReported() ; return }
                    } catch GatewayError.http(404, _) {
                        // No report has finished yet.
                    }
                    try await Task.sleep(for: .seconds(1))
                case .wait(let busy):
                    updating = busy
                    try await Task.sleep(for: .seconds(1))
                case .settle:
                    withAnimation(Motion.standard) { updating = false }
                    markReported()
                    return
                case .fail(let message):
                    throw GatewayError.message("Analysis failed: \(message)")
                }
            }
        } catch {
            // A report on screen stays; only the chip goes.
            if report != nil {
                updating = false
            } else {
                self.error = error.localizedDescription
            }
        }
    }

    /// Cached alongside the report JSON: the revision it is.
    static let revKey = "x_app_report_rev"

    private func markReported() {
        var r = app.storage.listSessions().first { $0.id == record.id } ?? record
        r.status = .reported
        try? app.storage.save(r)
    }
}

/// How long the "Searching for parts…" line waits for `fix_ready` (the backend's search budget plus slack).
enum FixSearch {
    static let giveUpS: Double = 75
}

// MARK: - Progress

/// Where the scan is on its way to a report, from the upload queue and GET /v1/scans/{id}/pipeline.
struct PipelineProgress: Equatable {
    enum Step: Int, CaseIterable {
        case upload, inspect, weigh, write

        var title: String {
            switch self {
            case .upload: "Sending photos"
            case .inspect: "Inspecting each photo"
            case .weigh: "Weighing the evidence"
            case .write: "Writing the report"
            }
        }
    }

    var step: Step = .upload
    var stillsDone = 0
    var stillsTotal = 0

    mutating func update(_ p: [String: Any]) {
        // A run lists every stage as pending from its start: a stage counts once it has started.
        let stages = p["stages"] as? [String: Any] ?? [:]
        func started(_ k: String) -> Bool {
            guard let status = (stages[k] as? [String: Any])?["status"] as? String else { return false }
            return status != "pending"
        }
        if started("report") { step = .write }
        else if started("fuse") || started("locate") { step = .weigh }
        else { step = .inspect }
        let stills = (p["stills"] as? [String: Any] ?? [:]).values.compactMap { ($0 as? [String: Any])?["status"] as? String }
        stillsTotal = stills.count
        stillsDone = stills.filter { $0 == "done" || $0 == "failed" }.count
    }
}

struct ReportProgressView: View {
    let progress: PipelineProgress
    let uploaded: Int
    let pending: Int
    let error: String?
    let retry: () -> Void

    var body: some View {
        ScrollView {
            VStack(spacing: Spacing.xl) {
                Spacer(minLength: Spacing.xl)
                ProgressOrb(fraction: fraction, failed: error != nil)
                VStack(spacing: Spacing.s) {
                    Text(error == nil ? "Getting your report" : "Something went wrong")
                        .font(.screenTitle).multilineTextAlignment(.center)
                    Text(error == nil ? "Lea looks at every photo, then writes up what it found." : (error ?? ""))
                        .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        .textSelection(.enabled)
                }
                VStack(spacing: 0) {
                    ForEach(PipelineProgress.Step.allCases, id: \.self) { step in
                        StepRow(title: step.title, detail: detail(step), state: state(step))
                        if step != .write { Divider().padding(.leading, 52) }
                    }
                }
                .card(padding: Spacing.s)
                if error != nil {
                    Button(action: retry) { Label("Try again", systemImage: "arrow.clockwise") }
                        .buttonStyle(.primary)
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .scrollBounceBehavior(.basedOnSize)
    }

    private var fraction: Double? {
        switch progress.step {
        case .upload: uploaded + pending > 0 ? Double(uploaded) / Double(uploaded + pending) : nil
        case .inspect: progress.stillsTotal > 0 ? Double(progress.stillsDone) / Double(progress.stillsTotal) : nil
        default: nil
        }
    }

    private func state(_ step: PipelineProgress.Step) -> StepRow.State {
        if step.rawValue < progress.step.rawValue { return .done }
        if step == progress.step { return error == nil ? .active : .failed }
        return .waiting
    }

    private func detail(_ step: PipelineProgress.Step) -> String? {
        switch step {
        case .upload where uploaded + pending > 0 && progress.step == .upload: "\(uploaded) of \(uploaded + pending) files"
        case .inspect where progress.stillsTotal > 0 && progress.step == .inspect: "\(progress.stillsDone) of \(progress.stillsTotal) photos"
        default: nil
        }
    }
}

private struct StepRow: View {
    enum State { case waiting, active, done, failed }
    let title: String
    let detail: String?
    let state: State

    var body: some View {
        HStack(spacing: Spacing.m) {
            ZStack {
                switch state {
                case .waiting: Circle().strokeBorder(Color.secondary.opacity(0.35), lineWidth: 1.5)
                case .active: ProgressView().controlSize(.small).tint(Palette.accent)
                case .done: Image(systemName: "checkmark.circle.fill").font(.title3).foregroundStyle(Palette.accent)
                case .failed: Image(systemName: "exclamationmark.circle.fill").font(.title3).foregroundStyle(Palette.danger)
                }
            }
            .frame(width: 26, height: 26)
            .padding(.leading, Spacing.s)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(state == .active ? .semibold : .regular))
                    .foregroundStyle(state == .waiting ? .secondary : .primary)
                if let detail { Text(detail).font(.footnote).monospacedDigit().foregroundStyle(.secondary) }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, Spacing.m)
        .accessibilityElement(children: .combine)
        .accessibilityValue(state == .done ? "done" : state == .active ? "in progress" : state == .failed ? "failed" : "waiting")
    }
}

/// A ring that fills with progress, or turns slowly while the step has no count. Static under Reduce Motion.
private struct ProgressOrb: View {
    let fraction: Double?
    let failed: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var spin = false

    var body: some View {
        ZStack {
            Circle().fill(tint.opacity(0.1))
            Circle().stroke(tint.opacity(0.18), lineWidth: 7).padding(14)
            Circle()
                .trim(from: 0, to: fraction.map { max(0.03, $0) } ?? 0.28)
                .stroke(tint, style: StrokeStyle(lineWidth: 7, lineCap: .round))
                .rotationEffect(.degrees(-90 + (fraction == nil && spin ? 360 : 0)))
                .padding(14)
                .motion(Motion.standard, value: fraction)
            Image(systemName: failed ? "exclamationmark" : "doc.text.magnifyingglass")
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(tint)
                .contentTransition(.symbolEffect(.replace))
        }
        .frame(width: 112, height: 112)
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.linear(duration: 1.6).repeatForever(autoreverses: false)) { spin = true }
        }
        .accessibilityHidden(true)
    }

    private var tint: Color { failed ? Palette.danger : Palette.accent }
}

// MARK: - Report

struct ReportContent: View {
    let report: ReportModel
    var symptom: String?
    var model: ModelSource = .none
    /// The session folder (its stills, for the photo hero).
    var sessionDir: URL?
    /// The scan's saved world map: "Show on object" relocalises into it.
    var space: ObjectSpace?
    var openFull: (() -> Void)?
    var fix: FixIt?
    var fixSearching = false
    var fixUnavailable = false
    @Environment(\.continueScan) private var continueScan
    @Environment(\.cropSource) private var cropSource
    @State var focus: String?
    @State private var sheet: ReportModel.Problem?
    @State private var showOnObject = false
    @State private var showOthers = false

    /// The top three located findings, numbered; the rest are behind the "+N" chip, not drawn as dots.
    private var pins: [ModelPin] {
        report.mostLikely.enumerated().compactMap { i, p in
            p.point.map { ModelPin(id: p.id, number: i + 1, point: $0, tone: p.severity.tone) }
        }
    }

    /// The top three with a photo crop, numbered (the photo hero).
    private var numberedPhotos: [PhotoHero.Mark] {
        report.mostLikely.enumerated().compactMap { i, p in
            guard let still = p.cropStill else { return nil }
            return PhotoHero.Mark(id: p.id, number: i + 1, still: still, box: p.cropBox, tone: p.severity.tone)
        }
    }

    /// Findings beyond the top three (lower-ranked and cosmetic).
    private var others: [ReportModel.Problem] { report.otherIssues + report.cosmetic }

    private var showsModel: Bool { !model.isNone || !pins.isEmpty }
    private var isPhotoHero: Bool { if case .photos = model { true } else { false } }
    private var canShowOnObject: Bool { space != nil && !pins.isEmpty }
    private var all: [ReportModel.Problem] { report.mostLikely + report.otherIssues + report.cosmetic }

    var body: some View {
        ScrollViewReader { proxy in
            content(proxy)
            #if DEBUG
                .task {
                    guard let anchor = UIGallery.scrollTo else { return }
                    try? await Task.sleep(for: .milliseconds(400))
                    proxy.scrollTo(anchor, anchor: .top)
                }
            #endif
        }
        .task {
            // Developer hook (`--show-on-object`): open the AR view once the report is up (unattended measurements).
            guard CommandLine.arguments.contains("--show-on-object"), canShowOnObject else { return }
            try? await Task.sleep(for: .seconds(1.5))
            showOnObject = true
        }
        .fullScreenCover(isPresented: $showOnObject) {
            if let space {
                ObjectMarkersView(space: space, markers: pins.map { ObjectMarker(id: $0.id, number: $0.number, point: $0.point, tone: $0.tone) },
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
    }

    private func content(_ proxy: ScrollViewProxy) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                if showsModel { modelCard.id("model") }
                VerdictHeader(report: report, symptom: symptom)
                if let lead = report.userLead { UserLeadCard(lead: lead) }
                if !report.safety.isEmpty { SafetyCard(notes: report.safety) }
                if !report.mostLikely.isEmpty {
                    VStack(alignment: .leading, spacing: Spacing.m) {
                        SectionHeader("Most likely problems")
                        ForEach(Array(report.mostLikely.enumerated()), id: \.element.id) { i, p in
                            if showsModel, isPhotoHero ? p.cropStill != nil : p.point != nil {
                                Button {
                                    withAnimation(Motion.standard) { proxy.scrollTo("model", anchor: .top) }
                                    focus = p.id
                                } label: {
                                    ProblemCard(problem: p, rank: i + 1, located: true, selected: focus == p.id)
                                }
                                .buttonStyle(PressableCardStyle())
                                .accessibilityHint("Shows it on the 3D model")
                            } else {
                                NavigationLink(value: p.id) { ProblemCard(problem: p, rank: i + 1, located: showsModel ? false : nil) }
                                    .buttonStyle(PressableCardStyle())
                            }
                        }
                    }
                }
                if fix != nil || fixSearching || fixUnavailable {
                    FixItCard(fix: fix, searching: fixSearching, unavailable: fixUnavailable).id("fix")
                }
                if let next = report.nextStep { NextStepCard(step: next).id("next") }
                if let summary = report.summary, !summary.isEmpty {
                    VStack(alignment: .leading, spacing: Spacing.m) {
                        SectionHeader("In plain words")
                        ClampedText(text: summary).card()
                    }
                }
                if !report.otherIssues.isEmpty {
                    ProblemList(title: "Other possible problems", problems: report.otherIssues).id("other")
                }
                if !report.cosmetic.isEmpty {
                    ProblemList(title: "Cosmetic", problems: report.cosmetic,
                                note: "Looks worn, but would not stop it working.")
                }
                if !report.notInspected.isEmpty {
                    GapList(title: "Not inspected", symbol: Symbol.notInspected, gaps: report.notInspected,
                            note: "Nobody looked at these, so they are not counted as fine.")
                        .id("gaps")
                }
                if !report.insufficient.isEmpty {
                    GapList(title: "Could not judge", symbol: Symbol.couldNotJudge, gaps: report.insufficient,
                            note: "Looked at, but the photo was not clear enough to say.")
                }
                if !report.limits.isEmpty { LimitsCard(limits: report.limits) }
                if let continueScan {
                    Button { continueScan() } label: { Label("Continue scanning", systemImage: Symbol.scan) }
                        .buttonStyle(.primary(.accent))
                }
                if let openFull {
                    Button(action: openFull) { Label("Full report with photos", systemImage: "doc.richtext") }
                        .buttonStyle(.primary(.quiet))
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .padding(.top, Spacing.s)
            .padding(.bottom, Spacing.xxl)
        }
        .navigationDestination(for: String.self) { id in
            if let p = all.first(where: { $0.id == id }) { ProblemDetailView(problem: p) }
        }
    }

    @Environment(\.dynamicTypeSize) private var typeSize

    /// "+N": the findings not drawn on the hero, as a list.
    @ViewBuilder private var othersChip: some View {
        if !others.isEmpty {
            Button { showOthers = true } label: {
                Text("+\(others.count)")
                    .font(.system(.subheadline, design: .rounded, weight: .bold)).monospacedDigit()
                    .foregroundStyle(Palette.accent)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Palette.surface, in: Capsule())
                    .overlay(Capsule().strokeBorder(Palette.hairline, lineWidth: 0.5))
            }
            .buttonStyle(PressableCardStyle())
            .padding(Spacing.m)
            .accessibilityLabel("\(others.count) more findings")
        }
    }

    /// The scan in 3D with the problem pins, and the selected problem under it.
    private var modelCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group {
                if case .photos = model, let sessionDir {
                    PhotoHero(stillsDir: sessionDir.appending(path: "stills"), numbered: numberedPhotos, focus: focus) { id in
                        focus = id
                        sheet = all.first { $0.id == id }
                    }
                } else {
                    ModelView(source: model, pins: pins, focus: focus, spin: true, showHint: !canShowOnObject) { id in
                        focus = id
                        sheet = all.first { $0.id == id }
                    }
                    .frame(height: 340)
                }
            }
            .overlay(alignment: .bottomLeading) {
                if canShowOnObject {
                    Button { showOnObject = true } label: { Label("Show on object", systemImage: "arkit") }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 14).padding(.vertical, 9)
                        .background(Palette.accentFill, in: Capsule())
                        .buttonStyle(PressableCardStyle())
                        .padding(Spacing.m)
                        .accessibilityHint("Places the numbered problems on the real object with the camera")
                }
            }
            .overlay(alignment: .bottomTrailing) { othersChip }
            if let id = focus, let p = all.first(where: { $0.id == id }) {
                Divider()
                let row = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.s))
                    : AnyLayout(HStackLayout(spacing: Spacing.m))
                row {
                    HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                        Circle().fill(p.severity.tone.color).frame(width: 10, height: 10).accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(p.part).font(.subheadline.weight(.semibold))
                            Text(p.issue).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    Spacer(minLength: 0)
                    Button("Details") { sheet = p }.font(.subheadline.weight(.semibold))
                }
                .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
                .transition(.opacity)
            }
            let unplaced = report.mostLikely.filter { $0.point == nil }.count
            if unplaced > 0, !isPhotoHero {
                Divider()
                Label(unplaced == report.mostLikely.count ? "These problems have no 3D position yet."
                      : "\(unplaced) of these problems \(unplaced == 1 ? "has" : "have") no 3D position.",
                      systemImage: "mappin.slash")
                    .font(.footnote).foregroundStyle(.secondary)
                    .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
            }
        }
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: 0.5))
        .motion(Motion.standard, value: focus)
    }
}

extension ReportModel.Severity? {
    var tone: Tone {
        switch self {
        case .safety: .danger
        case .cosmetic: .cosmetic
        case .functional, .none: .caution
        }
    }
}

private struct VerdictHeader: View {
    let report: ReportModel
    let symptom: String?
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let object = report.objectWords {
                Text(object).font(.pillLabel).foregroundStyle(.secondary).textCase(nil)
            }
            let row = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.s))
                : AnyLayout(HStackLayout(alignment: .center, spacing: Spacing.m))
            row {
                Image(systemName: symbol)
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 40, height: 40)
                    .background(tone.color, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .accessibilityHidden(true)
                Text(report.verdict.words).font(.screenTitle).fixedSize(horizontal: false, vertical: true)
            }
            if let reason = report.verdictReason {
                Text(reason).font(.subheadline).foregroundStyle(.secondary)
            }
            Text(meta).font(.footnote).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }

    private var meta: String {
        var parts: [String] = []
        if let symptom, !symptom.isEmpty { parts.append("“\(symptom)”") }
        if report.photos > 0 {
            parts.append(report.photosInspected == report.photos ? "\(report.photos) photos inspected"
                         : "\(report.photosInspected) of \(report.photos) photos inspected")
        }
        if let coverage = report.coverageWords { parts.append(coverage.capitalizedFirst) }
        if let date = report.createdAt { parts.append(date.formatted(.dateTime.month(.abbreviated).day().hour().minute())) }
        return parts.joined(separator: " · ")
    }

    private var tone: Tone {
        switch report.verdict {
        case .potentialIssues: report.mostLikely.contains { $0.severity == .safety } ? .danger : .caution
        case .looksGood, .noProblemsSeen: .good
        default: .cosmetic
        }
    }

    private var symbol: String {
        switch report.verdict {
        case .potentialIssues: Symbol.safety
        case .looksGood, .noProblemsSeen: Symbol.good
        default: Symbol.unknown
        }
    }
}

/// Long text cut to a few lines with "Read more".
private struct ClampedText: View {
    let text: String
    var lines = 6
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Text(text).font(.body).lineSpacing(3).lineLimit(open ? nil : lines).textSelection(.enabled)
            if text.count > 280 {
                Button(open ? "Show less" : "Read more") { withAnimation(Motion.standard) { open.toggle() } }
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.accent)
            }
        }
    }
}

private struct SafetyCard: View {
    let notes: [ReportModel.SafetyNote]

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s + 2) {
            Label("Before you touch it", systemImage: Symbol.safety)
                .font(.headline).foregroundStyle(Palette.danger)
            ForEach(notes) { note in
                HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
                    Image(systemName: note.symbol).foregroundStyle(Palette.danger).frame(width: 20).accessibilityHidden(true)
                    Text(note.text).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .card(tone: .danger)
        .accessibilityElement(children: .combine)
    }
}

private struct ProblemCard: View {
    let problem: ReportModel.Problem
    let rank: Int
    /// nil: no 3D view on this report. true: tapping flies to the pin. false: "No 3D position".
    var located: Bool?
    var selected = false
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.m))
            : AnyLayout(HStackLayout(alignment: .top, spacing: 14))
        layout {
            CropThumb(sha: problem.crops.first)
                .frame(width: typeSize.isAccessibilitySize ? nil : 88, height: typeSize.isAccessibilitySize ? 180 : 88)
                .overlay(alignment: .topLeading) {
                    Text("\(rank)")
                        .font(.system(.caption, design: .rounded, weight: .bold)).monospacedDigit()
                        .foregroundStyle(.white)
                        .frame(minWidth: 22, minHeight: 22)
                        .background(problem.severity.tone.color, in: Circle())
                        .padding(6)
                }
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline) {
                    Text(problem.part).font(.cardTitle).foregroundStyle(.primary)
                    Spacer(minLength: 0)
                    Image(systemName: located == true ? "scope" : "chevron.right")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(located == true ? AnyShapeStyle(Palette.accent) : AnyShapeStyle(.tertiary))
                }
                Text(problem.issue).font(.subheadline.weight(.semibold)).foregroundStyle(problem.severity.tone.color)
                if let words = problem.whereWords {
                    Text(words).font(.footnote).foregroundStyle(.secondary).lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                }
                ProblemChips(problem: problem, unplaced: located == false).padding(.top, 3)
            }
            .multilineTextAlignment(.leading)
        }
        .card(padding: 14)
        .overlay {
            RoundedRectangle(cornerRadius: Radius.card, style: .continuous)
                .strokeBorder(Palette.accent, lineWidth: 2)
                .opacity(selected ? 1 : 0)
        }
        .motion(Motion.fade, value: selected)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Problem \(rank): \(problem.part), \(problem.issue)")
        .accessibilityHint("Shows the photo and the evidence")
    }
}

private struct ProblemChips: View {
    let problem: ReportModel.Problem
    var all = false
    var unplaced = false

    var body: some View {
        FlowLayout(spacing: 6) {
            if let s = problem.severity { StatusPill(text: s.words, tone: Optional(s).tone) }
            if let c = problem.confidence { StatusPill(text: c) }
            if all, let l = problem.likelihood { StatusPill(text: "Likelihood: \(l.lowercased())") }
            if problem.regions > 1 { StatusPill(text: "Seen \(problem.regions) times", symbol: "square.on.square") }
            if problem.proved { StatusPill(text: "Measured", symbol: "gauge.with.dots.needle.50percent", tone: .accent) }
            if unplaced { StatusPill(text: "No 3D position", symbol: "mappin.slash") }
        }
    }
}

private struct NextStepCard: View {
    let step: ReportModel.NextStep

    var body: some View {
        HStack(alignment: .top, spacing: Spacing.m) {
            IconBadge(symbol: step.symbol, tone: .accent)
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text("Next step").font(.pillLabel).foregroundStyle(Palette.accent)
                Text(step.title).font(.headline)
                Text(step.instruction).font(.body).fixedSize(horizontal: false, vertical: true)
                if let reason = step.reason {
                    Text(reason).font(.footnote).foregroundStyle(.secondary).padding(.top, 2)
                }
            }
        }
        .card(tone: .accent)
        .accessibilityElement(children: .combine)
    }
}

/// A list card that shows the first few rows and expands in place.
private struct ExpandingList<Item: Identifiable, Row: View>: View {
    let items: [Item]
    var initial = 3
    @ViewBuilder var row: (Item) -> Row
    @State private var expanded = false

    var body: some View {
        VStack(spacing: 0) {
            let shown = expanded ? items : Array(items.prefix(initial))
            ForEach(Array(shown.enumerated()), id: \.element.id) { i, item in
                if i > 0 { Divider().padding(.leading, Spacing.l) }
                row(item)
            }
            if items.count > initial {
                Divider()
                Button {
                    withAnimation(Motion.standard) { expanded.toggle() }
                } label: {
                    HStack {
                        Text(expanded ? "Show fewer" : "Show all \(items.count)")
                        Spacer()
                        Image(systemName: "chevron.down").rotationEffect(.degrees(expanded ? 180 : 0))
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Palette.accent)
                    .padding(Spacing.l)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .background(Palette.surface, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.card, style: .continuous).strokeBorder(Palette.hairline, lineWidth: 0.5))
        .clipShape(RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
    }
}

private struct ProblemList: View {
    let title: String
    let problems: [ReportModel.Problem]
    var note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader(title, count: problems.count)
            if let note { Text(note).font(.footnote).foregroundStyle(.secondary) }
            ExpandingList(items: problems) { p in
                NavigationLink(value: p.id) {
                    HStack(spacing: Spacing.m) {
                        CropThumb(sha: p.crops.first, cornerRadius: Radius.small).frame(width: 48, height: 48)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p.part).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                            Text([p.issue, p.confidence].compactMap { $0 }.joined(separator: " · "))
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressableCardStyle())
            }
        }
    }
}

private struct GapList: View {
    let title: String
    let symbol: String
    let gaps: [ReportModel.Gap]
    let note: String

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            SectionHeader(title, count: gaps.count)
            Text(note).font(.footnote).foregroundStyle(.secondary)
            ExpandingList(items: gaps) { g in
                HStack(alignment: .top, spacing: Spacing.m) {
                    if let crop = g.crop {
                        CropThumb(sha: crop, cornerRadius: Radius.small).frame(width: 44, height: 44)
                    } else {
                        Image(systemName: symbol).font(.body).foregroundStyle(.secondary)
                            .frame(width: 44, height: 44)
                            .background(Palette.well, in: RoundedRectangle(cornerRadius: Radius.small, style: .continuous))
                            .accessibilityHidden(true)
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                            Text(g.part).font(.subheadline.weight(.semibold))
                            if let photo = g.photoLabel { Text(photo).font(.caption).foregroundStyle(.secondary) }
                        }
                        Text(g.reason).font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        if let detail = g.detail {
                            Text(detail).font(.footnote).foregroundStyle(.tertiary).lineLimit(3)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, Spacing.l).padding(.vertical, Spacing.m)
                .accessibilityElement(children: .combine)
            }
        }
    }
}

private struct LimitsCard: View {
    let limits: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            Label("What photos can't tell", systemImage: "info.circle").font(.subheadline.weight(.semibold))
            ForEach(limits, id: \.self) { line in
                Text(line).font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .card()
        .accessibilityElement(children: .combine)
    }
}

/// The findings beyond the top three, from the hero's "+N" chip (and the AR view's).
struct OtherFindingsSheet: View {
    let problems: [ReportModel.Problem]
    let open: (ReportModel.Problem) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List(problems) { p in
                Button { open(p) } label: {
                    HStack(spacing: Spacing.m) {
                        CropThumb(sha: p.crops.first, cornerRadius: Radius.small).frame(width: 48, height: 48)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(p.part).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                            Text([p.issue, p.confidence].compactMap { $0 }.joined(separator: " · "))
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                    }
                }
            }
            .navigationTitle("Other findings").navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() }.fontWeight(.semibold) }
        }
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Problem detail

struct ProblemDetailView: View {
    let problem: ReportModel.Problem

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                crops
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text(problem.part).font(.screenTitle)
                    Text(problem.issue).font(.title3.weight(.semibold)).foregroundStyle(problem.severity.tone.color)
                    ProblemChips(problem: problem, all: true).padding(.top, Spacing.xs)
                }
                if let observation = problem.observation {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text("What the photo shows").font(.headline)
                        Text(observation).font(.body).lineSpacing(2).textSelection(.enabled)
                        if !problem.proved {
                            Label("From photos only: a suspicion, not proof. A meter reading can confirm it.",
                                  systemImage: "info.circle")
                                .font(.footnote).foregroundStyle(.secondary).padding(.top, Spacing.xs)
                        }
                    }
                    .card()
                }
                if problem.whereWords != nil || problem.locationNote != nil || !problem.photoLabels.isEmpty {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text("Where").font(.headline)
                        if let words = problem.whereWords { Text(words).font(.body) }
                        if !problem.photoLabels.isEmpty {
                            Text(problem.photoLabels.joined(separator: ", ")).font(.footnote).foregroundStyle(.secondary)
                        }
                        if let note = problem.locationNote {
                            Text(note.capitalizedFirst).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    .card()
                }
                if !problem.evidence.isEmpty {
                    VStack(alignment: .leading, spacing: Spacing.s) {
                        Text("Evidence").font(.headline)
                        ForEach(problem.evidence, id: \.self) { line in
                            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                                Circle().fill(.tertiary).frame(width: 4, height: 4).accessibilityHidden(true)
                                Text(line.capitalizedFirst).font(.footnote).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .card()
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .padding(.bottom, Spacing.xxl)
        }
        .background(Palette.canvas.ignoresSafeArea())
        .navigationTitle(problem.part)
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder private var crops: some View {
        if problem.crops.count > 1 {
            TabView {
                ForEach(problem.crops, id: \.self) { sha in
                    CropThumb(sha: sha, cornerRadius: Radius.card).padding(.horizontal, 2)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .always))
            .aspectRatio(1, contentMode: .fit)
        } else if let sha = problem.crops.first {
            CropThumb(sha: sha, cornerRadius: Radius.card).aspectRatio(1, contentMode: .fit)
        }
    }
}

// MARK: - Plumbing

/// Wraps children onto new lines (pills, chips).
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(subviews, width: proposal.width ?? .infinity)
        return CGSize(width: rows.map(\.width).max() ?? 0, height: rows.last.map { $0.y + $0.height } ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for row in arrange(subviews, width: bounds.width) {
            for item in row.items {
                subviews[item.index].place(at: CGPoint(x: bounds.minX + item.x, y: bounds.minY + row.y),
                                           proposal: ProposedViewSize(width: item.width, height: row.height))
            }
        }
    }

    private struct Row { var y: CGFloat; var height: CGFloat = 0; var width: CGFloat = 0; var items: [(index: Int, x: CGFloat, width: CGFloat)] = [] }

    private func arrange(_ subviews: Subviews, width maxWidth: CGFloat) -> [Row] {
        var rows: [Row] = [Row(y: 0)]
        for (i, view) in subviews.enumerated() {
            let size = view.sizeThatFits(.unspecified)
            let w = min(size.width, maxWidth)
            if !rows[rows.count - 1].items.isEmpty, rows[rows.count - 1].width + spacing + w > maxWidth {
                let last = rows[rows.count - 1]
                rows.append(Row(y: last.y + last.height + spacing))
            }
            let x = rows[rows.count - 1].items.isEmpty ? 0 : rows[rows.count - 1].width + spacing
            rows[rows.count - 1].items.append((i, x, w))
            rows[rows.count - 1].width = x + w
            rows[rows.count - 1].height = max(rows[rows.count - 1].height, size.height)
        }
        return rows.filter { !$0.items.isEmpty }
    }
}

struct WebView: UIViewRepresentable {
    let request: URLRequest
    func makeUIView(context: Context) -> WKWebView {
        let v = WKWebView()
        v.load(request)
        return v
    }
    func updateUIView(_ v: WKWebView, context: Context) {}
}

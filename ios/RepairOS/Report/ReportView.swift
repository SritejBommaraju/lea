import SwiftUI
import WebKit

/// Upload and analysis progress, then the report: the scan on the stage with Lea's conversation (ReportStageView,
/// the caption strip), the report sheet below it, and the pinned Ask Lea bar. What wasn't checked is one tap away in
/// the ⋯ menu, never counted as fine. Confidence is a word, never a number (A5).
struct ReportView: View {
    @EnvironmentObject private var app: AppState
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
    /// Bumped by Lea's `walkthrough` action ("Lea, walk me through it"): the stage plays its tour.
    @State private var walkthrough = 0
    /// Lea's latest move of the 3D model while she answers (LeaStage): the stage focuses, rings and flies to it.
    @State private var leaCue: LeaStageCue?
    /// The conversation as the stage's caption strip shows it (fed by the pinned Ask Lea bar).
    @StateObject private var captions = LeaCaptionFeed()
    @State private var showUnchecked = false
    @State private var showOnObject = false
    /// The meter-reading sheet, and how many readings this report has sent.
    @State private var meterContext: MeterReadingContext?
    @State private var meterReadingsSent = 0
    @State private var showFixWithGrain = false
    @Environment(\.continueScan) private var continueScan
    @Environment(\.scanBottom) private var scanBottom
    @Environment(\.liveEvents) private var live

    private var gateway: GatewayClient? { offline ? nil : app.gateway }

    var body: some View {
        Watching(live) { Watching(upload) { reportBody } }
    }

    private var reportBody: some View {
        NavigationStack {
            Group {
                if let report {
                    ReportStageView(report: report, symptom: record.symptom, model: model, sessionDir: app.storage.dir(record),
                                    space: space,
                                    openFull: gateway == nil ? nil : { showFull = true },
                                    fix: live?.fix ?? report.fix,
                                    fixSearching: fixSearching && live?.fix == nil && live?.fixEnded == nil && report.fix == nil,
                                    fixUnavailable: (fixLimited || live?.fixEnded == "limited") && live?.fix == nil && report.fix == nil,
                                    fresh: landedFresh, voiceStarted: voiceStarted, walkthrough: walkthrough, leaCue: leaCue,
                                    captions: talking ? captions : nil,
                                    identity: Self.identity(corrected: record.objectName, report: report.objectWords,
                                                            live: live?.identity?.words),
                                    addMeterReading: gateway == nil ? nil : {
                                        meterContext = report.nextStep.map { MeterReadingContext(step: $0) } ?? MeterReadingContext()
                                    },
                                    askDesign: gateway == nil || report.design == nil ? nil : { showFixWithGrain = true },
                                    meterReadingsSent: meterReadingsSent)
                        .transition(.opacity)
                } else if error == nil {
                    // While the Mac works: the person's own scan on the stage, one line of progress.
                    ReportStageWaiting(model: model, progress: progress, uploaded: upload?.uploadedFiles ?? 0,
                                       pending: upload?.pending.count ?? 0, found: live?.areasChecked)
                } else {
                    ReportProgressView(progress: progress, uploaded: upload?.uploadedFiles ?? 0,
                                       pending: upload?.pending.count ?? 0, error: error, retry: retry)
                }
            }
            .overlay(alignment: .top) { if report != nil || error == nil { topBar } }
            .motion(Motion.standard, value: report == nil)
            .safeAreaInset(edge: .bottom) { if let report { pinnedBar(report) } }
            .background(Palette.canvas.ignoresSafeArea())
            // The stage draws its own chalk-on-smoke controls (no glass over the scan); the error screen keeps the
            // system bar with Done.
            .toolbar(report == nil && error != nil ? .visible : .hidden, for: .navigationBar)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done", action: close).fontWeight(.semibold) }
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
                    .tint(Palette.accent)
                }
            }
            .sheet(isPresented: $showUnchecked) {
                if let report {
                    WhatWasntCheckedSheet(notInspected: report.notInspected, unclear: report.insufficient, limits: report.limits,
                                          continueScan: continueScan.map { action -> () -> Void in { action() } })
                }
            }
            .sheet(isPresented: $showFixWithGrain) {
                FixWithGrainSheet(scanID: record.scanID, symptom: record.symptom, gateway: gateway)
            }
            .sheet(item: $meterContext) { ctx in
                MeterReadingSheet(context: ctx, scanID: record.scanID,
                                  sessionID: upload?.state.gatewaySessionID ?? record.id, gateway: gateway) {
                    meterReadingsSent += 1
                }
            }
            .fullScreenCover(isPresented: $showOnObject) {
                if let report, let space {
                    let all = report.mostLikely + report.otherIssues + report.setup + report.cosmetic
                    ObjectMarkersView(space: space,
                                      markers: StageMarks.pins(report).map { ObjectMarker(id: $0.id, number: $0.number, point: $0.point, tone: $0.tone) },
                                      problems: all, others: report.otherIssues + report.cosmetic,
                                      show3D: { showOnObject = false }, close: { showOnObject = false })
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
            .onValueChange(of: live?.fix) { _, fix in if let fix { keepFix(fix) } }
        }
        .tint(Palette.accent)
        .environment(\.cropSource, CropSource.saved(in: CropSource.dir(app.storage.dir(record)), fallback: gateway.map(CropSource.gateway)))
    }

    /// What the object is, for the stage: the person's own correction first, then whichever identification names the
    /// product ("Category · Brand Model" over a bare category), the report's before the live event's.
    nonisolated static func identity(corrected: String?, report: String?, live: String?) -> String? {
        if let c = corrected?.trimmedOrNil { return c }
        let options = [report, live].compactMap { $0?.trimmedOrNil }
        return options.first { $0.contains("·") } ?? options.first
    }

    /// The scan's place in the room for "Show on the real object" (nil without a LiDAR world map).
    private var space: ObjectSpace? {
        ObjectSpace.load(sessionDir: app.storage.dir(record), scanPackage: app.storage.scanPackageURL(record))
    }

    /// Lea can be asked about this report (she needs the paired server: her answers are grounded in its report).
    private var talking: Bool { askGateway != nil }

    private var askGateway: GatewayClient? {
        if let gateway, app.isPaired { return gateway }
        #if DEBUG
        // Design review (`--ui-ask`): the bar as a paired phone shows it, pointed at nowhere reachable.
        if CommandLine.arguments.contains("--ui-ask") { return GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil) }
        #endif
        return nil
    }

    /// Over the stage: "‹ Scans", the voice toggle and the ⋯ menu as chalk-on-smoke controls, and "Updating…" while a
    /// newer revision is built.
    private var topBar: some View {
        VStack(spacing: Spacing.s) {
            HStack(spacing: Spacing.s) {
                Button(action: close) { Label("Scans", systemImage: "chevron.left") }
                    .buttonStyle(StageCapsuleButtonStyle())
                    .accessibilityLabel("Back to scans")
                Spacer()
                VoiceMuteButton(onCamera: true)
                Menu { moreMenu } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 17)).foregroundStyle(Palette.chalk)
                        .frame(width: 44, height: 44)
                        .background { SmokeFill(shape: Circle()) }
                        .contentShape(Circle())
                }
                .accessibilityLabel("More")
            }
            if updating {
                Label("Updating…", systemImage: "arrow.triangle.2.circlepath").cameraChip()
                    .transition(.opacity)
            }
        }
        .padding(.horizontal, Spacing.l)
        .padding(.top, Spacing.xs)
        .motion(Motion.fade, value: updating)
    }

    /// The ⋯ menu: what is off the default path (owner, 2026-10-25: no caveats by default).
    @ViewBuilder private var moreMenu: some View {
        if let report {
            if space != nil, !StageMarks.pins(report).isEmpty {
                Button("Show on the real object", systemImage: "arkit") { showOnObject = true }
            }
            if let scanBottom {
                Button("Scan the bottom", systemImage: "arrow.up.arrow.down") { scanBottom() }
            }
            if ReportWords.unchecked(report) > 0 {
                Button("What wasn't checked", systemImage: Symbol.couldNotJudge) { showUnchecked = true }
            }
            if gateway != nil {
                Button("Full report with photos", systemImage: "doc.richtext") { showFull = true }
            }
        }
        Button("Delete scan", systemImage: "trash", role: .destructive) { pendingDelete = record }
            .disabled(offline || !ScanLibrary.canDelete(record, app: app))
    }

    /// The report's one next thing, pinned in the frosted bar: tap to talk to Lea about this scan beside Continue
    /// scanning (blue). Not paired: Continue scanning alone, when it can go.
    @ViewBuilder private func pinnedBar(_ report: ReportModel) -> some View {
        if let gateway = askGateway {
            ConversationPanel(scanID: record.scanID, gateway: gateway, diag: Diag.session(app.storage.dir(record)),
                              sessionID: { [upload] in upload?.state.gatewaySessionID },
                              onStage: { command in leaCue = LeaStageCue(n: (leaCue?.n ?? 0) + 1, command: command) },
                              skin: .bar, feed: captions, continueScan: continueScan != nil) { kind in
                if kind == "continue_scan" { continueScan?() }
                if kind == "walkthrough" { walkthrough += 1 }
            }
        } else if let continueScan {
            GlassBar {
                Button { continueScan() } label: { Label("Continue scanning", systemImage: Symbol.scan) }
                    .buttonStyle(.primary)
            }
        }
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
    private var heroChoice: String? { Self.heroChoice(live: live?.hero, pipeline: pipelineHero, cached: report?.hero) }

    /// A reopened report never polls the pipeline, so the cached report's choice has to count as much as a live one.
    nonisolated static func heroChoice(live: String?, pipeline: String?, cached: String?) -> String? { live ?? pipeline ?? cached }

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

/// Report progress or a failure (DESIGN §7.4, screen 14): the construction orb with the real fraction, the
/// pipeline's own step names as a numbered index, and Try again. Normal waiting shows the scan on the stage instead
/// (ReportStageWaiting).
struct ReportProgressView: View {
    let progress: PipelineProgress
    let uploaded: Int
    let pending: Int
    let error: String?
    let retry: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.xl) {
                ConstructionOrb(fraction: error == nil ? fraction : (fraction ?? 1), failed: error != nil)
                    .frame(maxWidth: .infinity)
                    .padding(.top, Spacing.xl)
                VStack(alignment: .leading, spacing: Spacing.s) {
                    Text(error == nil ? "Writing your report" : "Something went wrong")
                        .display(.screen).foregroundStyle(Palette.ink)
                        .accessibilityAddTraits(.isHeader)
                    Text(error == nil ? "Lea looks at every photo, then writes up what it found." : (error ?? ""))
                        .font(.body).foregroundStyle(error == nil ? Palette.secondary : Palette.danger)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                VStack(spacing: 0) {
                    ForEach(PipelineProgress.Step.allCases, id: \.self) { step in
                        if step != .upload { Rectangle().fill(Palette.hairline).frame(height: Line.hairline) }
                        StepRow(number: step.rawValue + 1, title: step.title, detail: detail(step), state: state(step))
                    }
                }
                if error != nil {
                    Button(action: retry) { Label("Try again", systemImage: "arrow.clockwise") }
                        .buttonStyle(.primary)
                } else {
                    Text("You can leave this screen. The report appears in Scans when it is ready.")
                        .font(.footnote).foregroundStyle(Palette.secondary)
                }
            }
            .padding(.horizontal, Spacing.gutter)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .bounceBasedOnSize()
        .background(Palette.canvas.ignoresSafeArea())
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

/// One step as an index line: its number in a fixed 20 pt column, the title, then its state (an ink check when done,
/// a small spinner while active, an empty circle while waiting, the warning in danger when it failed).
private struct StepRow: View {
    enum State { case waiting, active, done, failed }
    let number: Int
    let title: String
    let detail: String?
    let state: State
    @ScaledMetric(relativeTo: .subheadline) private var column: CGFloat = 20

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Spacing.m) {
            Text("\(number)").display(.number).foregroundStyle(state == .waiting ? Palette.secondary : Palette.ink)
                .frame(width: column, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(state == .active ? .semibold : .regular))
                    .foregroundStyle(state == .done || state == .waiting ? Palette.secondary : Palette.ink)
                if let detail { Text(detail).font(.footnote).monospacedDigit().foregroundStyle(Palette.secondary) }
            }
            Spacer(minLength: 0)
            Group {
                switch state {
                case .waiting: Circle().strokeBorder(Palette.secondary.opacity(0.6), lineWidth: Line.construction).frame(width: 16, height: 16)
                case .active: ProgressView().controlSize(.small).tint(Palette.ink)
                case .done: Image(systemName: "checkmark").foregroundStyle(Palette.ink)
                case .failed: Image(systemName: Symbol.safety).foregroundStyle(Palette.danger)
                }
            }
            .frame(width: 24)
        }
        .padding(.vertical, Spacing.m)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityValue(state == .done ? "done" : state == .active ? "in progress" : state == .failed ? "failed" : "waiting")
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

extension ReportModel.Problem {
    /// Its colour: a setup state (not broken) is the calm setup tone, unless it is a hazard; else by severity.
    var tone: Tone { isSetup && severity != .safety ? .setup : severity.tone }
}

/// "Also worth a look", all of it: one grouped row each (crop, part, issue in its tone), tap for the detail.
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
                            Text(p.part).font(.subheadline.weight(.semibold)).foregroundStyle(Palette.ink)
                            Text(p.issue).font(.footnote.weight(.semibold)).foregroundStyle(p.tone.text)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                    }
                    .frame(minHeight: 44)
                }
                .listRowBackground(Palette.surface)
            }
            .settingsList()
            .navigationTitle("Also worth a look").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.fontWeight(.semibold) } }
        }
        .tint(Palette.accent)
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Plumbing

struct WebView: UIViewRepresentable {
    let request: URLRequest
    func makeUIView(context: Context) -> WKWebView {
        let v = WKWebView()
        v.load(request)
        return v
    }
    func updateUIView(_ v: WKWebView, context: Context) {}
}

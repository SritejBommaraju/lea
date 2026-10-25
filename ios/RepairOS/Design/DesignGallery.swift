#if DEBUG
import SwiftUI

/// DEBUG only: opens one screen straight from launch arguments, for simulator screenshots and design review.
///
///     --ui-screen home|sessions|report|report3d|detail|progress|settings|pairing|consent|overlay
///     --ui-fixture <dir>    a folder holding a report JSON (report*.json) and `blobs/<aa>/<sha>` crops,
///                           kept outside git; without it the report is spec/contracts/examples/report.json.
///     --ui-screen stage|stage-landing|stage-wait  --ui-session <dir>
///                           the dark-stage report (Report/Stage) from a real saved session folder copied off the
///                           phone (report.json, crops/, scans/<id>/*.ply); `stage-landing` plays the landing moment.
enum UIGallery {
    static var screen: String? { value("--ui-screen") }
    /// `--ui-scroll next|other|gaps`: scroll the report to that section.
    static var scrollTo: String? { value("--ui-scroll") }
    /// `--ui-focus`: start the 3D report focused on the first pin.
    static var focusFirst: Bool { CommandLine.arguments.contains("--ui-focus") }

    @MainActor @ViewBuilder
    static func root(_ screen: String, app: AppState) -> some View {
        let crops = fixture.map { CropSource.folder($0.appending(path: "blobs")) } ?? .none
        let report = report()
        Group {
            switch screen {
            case "sessions":
                RootView().task { addSampleSessions(app) }
            case "report":
                NavigationStack {
                    ReportContent(report: report, symptom: "Stopped working after a drop", openFull: {})
                        .background(Palette.canvas.ignoresSafeArea())
                        .navigationTitle("Report").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                }
                .tint(Palette.accent)
            case "report3d":
                NavigationStack {
                    ReportContent(report: located(report), symptom: "Stopped working after a drop",
                                  model: .points(SyntheticScan.cloud), openFull: {},
                                  focus: focusFirst ? report.mostLikely.first?.id : nil)
                        .background(Palette.canvas.ignoresSafeArea())
                        .navigationTitle("Report").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                }
                .tint(Palette.accent)
            case "detail":
                NavigationStack {
                    if let p = report.mostLikely.first { ProblemDetailView(problem: p) }
                }
                .tint(Palette.accent)
            case "progress":
                NavigationStack {
                    ReportProgressView(progress: PipelineProgress(step: .inspect, stillsDone: 5, stillsTotal: 13),
                                       uploaded: 40, pending: 0, error: nil, retry: {})
                        .background(Palette.canvas.ignoresSafeArea())
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                }
            case "stage", "stage-landing":
                NavigationStack {
                    if let dir = session {
                        let report = sessionReport(dir)
                        ReportStageView(report: report, symptom: CommandLine.arguments.contains("--ui-no-story") ? nil : "Stopped heating",
                                        model: sessionModel(dir), sessionDir: dir, openFull: {}, fresh: screen == "stage-landing",
                                        hero: sessionHero(dir), focus: focusFirst ? report.mostLikely.first?.id : nil)
                            .navigationTitle("").navigationBarTitleDisplayMode(.inline)
                            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                            // `--ui-build-shell`: build the phone's recreation shell for this session (next launch shows it).
                            .task {
                                guard CommandLine.arguments.contains("--ui-build-shell"), let id = sessionScanID(dir) else { return }
                                RecreationCenter.shared.buildShell(sessionDir: dir, scanID: id)
                            }
                    }
                }
                .tint(Palette.accent)
                .environment(\.cropSource, session.map { CropSource.saved(in: CropSource.dir($0), fallback: blobs.map(CropSource.folder)) } ?? crops)
            case "stage-old":
                // The same real session in the previous report layout, for before/after.
                NavigationStack {
                    if let dir = session {
                        ReportContent(report: sessionReport(dir), symptom: "Stopped heating", model: sessionModel(dir),
                                      sessionDir: dir, openFull: {})
                            .background(Palette.canvas.ignoresSafeArea())
                            .navigationTitle("Report").navigationBarTitleDisplayMode(.inline)
                            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                    }
                }
                .tint(Palette.accent)
                .environment(\.cropSource, session.map { CropSource.saved(in: CropSource.dir($0), fallback: nil) } ?? crops)
            case "stage-wait":
                NavigationStack {
                    ReportStageWaiting(model: session.map(sessionModel) ?? .none,
                                       progress: PipelineProgress(step: .inspect, stillsDone: 8, stillsTotal: 13),
                                       uploaded: 40, pending: 0, found: 1)
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                }
            case "recreation": RecreationGalleryScreen()   // Report/Recreation (photo-real 3D)
            case "settings": SettingsView()
            case "pairing": PairingView()
            case "consent": ConsentView { _ in }
            case "overlay": CaptureOverlayPreview(backdrop: backdrop())
            default: RootView()
            }
        }
        .environment(\.cropSource, crops)
    }

    // MARK: - Fixture

    static var fixture: URL? { value("--ui-fixture").map { URL(fileURLWithPath: $0, isDirectory: true) } }

    /// `--ui-session <dir>`: a real saved session folder (as on the phone) for the stage screens.
    static var session: URL? { value("--ui-session").map { URL(fileURLWithPath: $0, isDirectory: true) } }

    static func sessionReport(_ dir: URL) -> ReportModel {
        let json = (try? Data(contentsOf: dir.appending(path: "report.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        return ReportModel(json: json ?? [:])
    }

    /// `--ui-blobs <dir>`: a gateway blob store (`<aa>/<sha>`) to fill the session's crops from.
    static var blobs: URL? { value("--ui-blobs").map { URL(fileURLWithPath: $0, isDirectory: true) } }

    static func sessionScanID(_ dir: URL) -> String? {
        let json = (try? Data(contentsOf: dir.appending(path: "report.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        return json?["scan_id"] as? String
    }

    /// The recreation hero when the session's scan has one (same rule as ReportView.stageHero).
    @MainActor static func sessionHero(_ dir: URL) -> StageHero {
        guard let id = sessionScanID(dir) else { return .automatic }
        let scanDir = RecreationCenter.scanDir(sessionDir: dir, scanID: id)
        guard RecreationCenter.shared.available(scanDir) else { return .automatic }
        return .custom { ctx in
            AnyView(RecreationView(source: .scan(scanDir), markers: ctx.markers, focus: ctx.focus,
                                   onMarkerTap: ctx.select, onLoaded: ctx.ready, probe: ctx.probe))
        }
    }

    /// The session's own 3D: the first scan folder holding a model.
    static func sessionModel(_ dir: URL) -> ModelSource {
        let scans = dir.appending(path: "scans")
        let ids = ((try? FileManager.default.contentsOfDirectory(atPath: scans.path)) ?? []).sorted()
        for id in ids {
            let found = ModelSource.find(in: scans.appending(path: id))
            if !found.isNone { return found }
        }
        return .none
    }

    static func report() -> ReportModel {
        let fm = FileManager.default
        var url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appending(path: "../../../spec/contracts/examples/report.json").standardized
        if let dir = fixture, let name = (try? fm.contentsOfDirectory(atPath: dir.path))?
            .first(where: { $0.hasPrefix("report") && $0.hasSuffix(".json") }) {
            url = dir.appending(path: name)
        }
        let json = (try? Data(contentsOf: url)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        return ReportModel(json: json ?? [:])
    }

    /// The report with synthetic 3D positions on its top problems (there is no real located report yet).
    static func located(_ r: ReportModel) -> ReportModel {
        var r = r
        for i in r.mostLikely.indices.prefix(2) { r.mostLikely[i].point = SyntheticScan.points[i] }
        return r
    }

    /// The largest photo in the fixture's blobs, as a stand-in camera picture behind the overlay.
    static func backdrop() -> UIImage? {
        guard let blobs = fixture?.appending(path: "blobs"),
              let files = FileManager.default.enumerator(at: blobs, includingPropertiesForKeys: [.fileSizeKey]) else { return nil }
        let photos = files.compactMap { $0 as? URL }.filter { $0.pathExtension.isEmpty }
        let biggest = photos.max {
            ((try? $0.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) < ((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
        return biggest.flatMap { UIImage(contentsOfFile: $0.path) }
    }

    /// Writes a few local session records (simulator design review only) so the home list has rows.
    @MainActor
    static func addSampleSessions(_ app: AppState) {
        guard app.sessions.isEmpty else { return }
        let samples: [(String?, SessionRecord.Status, Int)] = [
            ("Stopped working after a drop", .reported, 13), ("Won't power on", .uploaded, 9), (nil, .captured, 6),
        ]
        for (symptom, status, photos) in samples {
            guard var r = try? app.storage.newSession(symptom: symptom, engine: app.capabilities.engine) else { continue }
            r.status = status
            r.stillIDs = (1...photos).map { "s\($0)" }
            try? app.storage.save(r)
        }
        app.reloadSessions()
    }

    private static func value(_ flag: String) -> String? {
        let args = CommandLine.arguments
        return args.firstIndex(of: flag).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
    }
}

#Preview("Report (spec example)") {
    NavigationStack { ReportContent(report: UIGallery.report(), symptom: "Stopped working after a drop") }
}

#Preview("Progress") {
    ReportProgressView(progress: PipelineProgress(step: .upload), uploaded: 12, pending: 18, error: nil, retry: {})
}
#endif

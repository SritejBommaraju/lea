#if DEBUG
import CryptoKit
import SwiftUI

/// DEBUG only: opens one screen straight from launch arguments, for simulator screenshots and design review.
///
///     --ui-screen home|progress|settings|pairing|consent|talk|recreation
///     --ui-screen report [--ui-record <session id>] [--ui-ask]
///                           the real report screen for a session saved in the app, offline (its top bar, sheet and,
///                           with `--ui-ask`, the pinned Ask Lea bar).
///     --ui-screen stage|stage-landing|stage-wait|detail|overlay  --ui-session <dir>
///                           from a real saved session folder copied off the phone (report.json, crops/, stills/,
///                           scans/<id>/*.ply, app_session.json): the stage report (`stage-landing` plays the landing
///                           moment), the first problem's detail, and the capture overlay over one of its real stills.
///     --ui-walkthrough       on the stage screens: start "Walk me through it" two seconds after opening.
///     --ui-locked            on `overlay`: the locked state.
///     --ui-select            on Home: selection mode with the first deletable scan ticked.
///     --ui-lea-replay <eval.json> --ui-lea-turn <id>
///                           on `stage`: replays one recorded real report turn (eval/live_vision results: the question,
///                           Lea's words with their `<show/>` markers, and the 3D points the gateway resolved them to)
///                           through the caption strip and the hero's camera, so Lea's moves can be reviewed and
///                           recorded without a live gateway. Sentence timing is paced at 14 characters a second.
/// Home with rows reads the real sessions in the app's Documents/sessions; nothing here writes sample data.
enum UIGallery {
    static var screen: String? { value("--ui-screen") }
    /// `--ui-scroll next|other|gaps`: scroll the report to that section.
    static var scrollTo: String? { value("--ui-scroll") }
    /// `--ui-focus`: start the 3D report focused on the first pin.
    static var focusFirst: Bool { CommandLine.arguments.contains("--ui-focus") }

    @MainActor @ViewBuilder
    static func root(_ screen: String, app: AppState) -> some View {
        let crops = blobs.map(CropSource.folder) ?? .none
        Group {
            switch screen {
            case "detail":
                NavigationStack {
                    if let dir = session {
                        let r = sessionReport(dir)
                        let list = r.mostLikely + r.setup
                        if !list.isEmpty {
                            ProblemDetailView(problems: list, stillsDir: dir.appending(path: "stills"),
                                              numbers: Dictionary(list.enumerated().map { ($0.element.id, $0.offset + 1) },
                                                                  uniquingKeysWith: { a, _ in a }),
                                              onDone: {})
                        }
                    }
                }
                .tint(Palette.accent)
                .environment(\.cropSource, session.map { CropSource.saved(in: CropSource.dir($0), fallback: blobs.map(CropSource.folder)) } ?? crops)
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
                        GalleryWalkthrough { walk in
                            GalleryLeaReplay(sessionDir: dir) { cue, feed in
                                ReportStageView(report: report, symptom: CommandLine.arguments.contains("--ui-no-story") ? nil : sessionSymptom(dir),
                                                model: sessionModel(dir), sessionDir: dir, openFull: {}, fresh: screen == "stage-landing",
                                                walkthrough: walk, leaCue: cue, hero: sessionHero(dir), captions: feed,
                                                focus: focusFirst ? report.mostLikely.first?.id : nil)
                            }
                        }
                            .toolbar(.hidden, for: .navigationBar)   // the report draws its own controls over the stage
                            // `--ui-build-shell`: build the phone's recreation shell for this session (next launch shows it).
                            .task {
                                guard CommandLine.arguments.contains("--ui-build-shell"), let id = sessionScanID(dir) else { return }
                                RecreationCenter.shared.buildShell(sessionDir: dir, scanID: id)
                            }
                    }
                }
                .tint(Palette.accent)
                .environment(\.cropSource, session.map { CropSource.saved(in: CropSource.dir($0), fallback: blobs.map(CropSource.folder)) } ?? crops)
            case "stage-wait":
                NavigationStack {
                    ReportStageWaiting(model: session.map(sessionModel) ?? .none,
                                       progress: PipelineProgress(step: .inspect, stillsDone: 8, stillsTotal: 13),
                                       uploaded: 40, pending: 0, found: 1)
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") {}.fontWeight(.semibold) } }
                }
            case "report":
                // The real report screen for a session saved in the app (`--ui-record <id>`, else the newest), offline:
                // the cached report and the scan's own files. `--ui-ask` shows the pinned Ask Lea bar as when paired.
                if let record = value("--ui-record").flatMap({ id in app.sessions.first { $0.id == id } }) ?? app.sessions.first {
                    ReportView(record: record, upload: nil, close: {}, offline: true)
                }
            case "recreation": RecreationGalleryScreen()   // Report/Recreation (photo-real 3D)
            case "settings": SettingsView()
            case "pairing": PairingView()
            case "consent": ConsentView { _ in }
            case "overlay": CaptureOverlayPreview(backdrop: backdrop(), locked: CommandLine.arguments.contains("--ui-locked"))
            case "talk":
                // The conversation sheet, idle. Unpaired-safe: without a paired server it points nowhere reachable.
                Color.clear.sheet(isPresented: .constant(true)) {
                    HomeConversationSheet(gateway: app.gateway ?? GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil)) { _ in }
                }
            default: RootView()
            }
        }
        .environment(\.cropSource, crops)
    }

    // MARK: - Session

    /// `--ui-session <dir>`: a real saved session folder (as on the phone) for the stage screens.
    static var session: URL? { value("--ui-session").map { URL(fileURLWithPath: $0, isDirectory: true) } }

    /// The person's own words for the session, from its `app_session.json` (nil when they gave none).
    static func sessionSymptom(_ dir: URL) -> String? {
        let json = (try? Data(contentsOf: dir.appending(path: "app_session.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        return (json?["symptom"] as? String)?.trimmedOrNil
    }

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
                                   onMarkerTap: ctx.select, onLoaded: ctx.ready, probe: ctx.probe, tour: ctx.tour))
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

    /// The largest real still in the session (`--ui-session <dir>/stills`), upright, as the camera picture behind the
    /// overlay. Sensor stills are landscape; the phone was upright.
    static func backdrop() -> UIImage? {
        guard let stills = session?.appending(path: "stills"),
              let files = try? FileManager.default.contentsOfDirectory(at: stills, includingPropertiesForKeys: [.fileSizeKey]) else { return nil }
        let photos = files.filter { $0.pathExtension.lowercased() == "jpg" }
        let biggest = photos.max {
            ((try? $0.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) < ((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
        guard let image = biggest.flatMap({ UIImage(contentsOfFile: $0.path) }), let cg = image.cgImage else { return nil }
        return cg.width > cg.height ? UIImage(cgImage: cg, scale: 1, orientation: .right) : image
    }

    static func value(_ flag: String) -> String? {
        let args = CommandLine.arguments
        return args.firstIndex(of: flag).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
    }
}

/// `--ui-lea-replay`: plays a recorded real turn (question, Lea's sentences, her moves with their resolved 3D points)
/// into the stage, as the conversation would. Nothing is invented: the words and points come from the eval record;
/// the photo hand-off uses the still the record cites, saved by its own SHA-256 in the session copy's crops.
private struct GalleryLeaReplay<Content: View>: View {
    let sessionDir: URL
    @ViewBuilder let content: (LeaStageCue?, LeaCaptionFeed?) -> Content
    @State private var cue: LeaStageCue?
    @State private var feed = LeaCaptionFeed()
    private var path: String? { UIGallery.value("--ui-lea-replay") }

    var body: some View {
        content(cue, path == nil ? nil : feed).task {
            guard let path, let turn = Self.turn(path: path, id: UIGallery.value("--ui-lea-turn")) else { return }
            await play(turn)
        }
    }

    struct Move { var id: String; var label: String; var point: SIMD3<Float>; var pixel: (photo: String, x: Double, y: Double)? }

    struct Turn {
        var question: String
        var sentences: [(text: String, moves: [Move])]
        var photo: String?
    }

    static func turn(path: String, id: String?) -> Turn? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let all = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]],
              let t = all.first(where: { id == nil || $0["id"] as? String == id }),
              let question = t["question"] as? String, let raw = t["raw"] as? String else { return nil }
        let points = (t["points"] as? [[String: Any]]) ?? []
        var queue: [Move] = points.compactMap { p in
            guard let m = p["marker"] as? [String: Any], let part = m["part"] as? String ?? p["label"] as? String,
                  let xyz = p["point"] as? [NSNumber], xyz.count == 3 else { return nil }
            // Lea's own pointer in a photo (`<point photo x y/>`, upright fractions), when the turn has one.
            var pixel: (String, Double, Double)?
            if let photo = m["photo"] as? String, let x = (m["x"] as? NSNumber)?.doubleValue, let y = (m["y"] as? NSNumber)?.doubleValue {
                pixel = (photo, x, y)
            }
            return Move(id: part, label: p["label"] as? String ?? part, point: SIMD3(xyz[0].floatValue, xyz[1].floatValue, xyz[2].floatValue),
                        pixel: pixel)
        }
        // Sentences of the raw answer, cut and cleaned the way the gateway and the phone do it (LeaText: a stop only
        // before a space, so "1.5 mm" stays whole; markers, image names and ids never shown); a marker belongs to the
        // sentence it opens or sits in.
        var out: [(String, [Move])] = []
        let flat = raw.replacingOccurrences(of: "\n", with: " ")
        var current = "", moves: [Move] = []
        var i = flat.startIndex
        while i < flat.endIndex {
            if flat[i] == "<", let close = flat[i...].firstIndex(of: ">") {
                if !queue.isEmpty, flat[i...close].contains("part=") || flat[i...close].contains("point") {
                    moves.append(queue.removeFirst())
                }
                i = flat.index(after: close)
                continue
            }
            current.append(flat[i])
            let next = flat.index(after: i)
            if ".!?".contains(flat[i]), next == flat.endIndex || flat[next].isWhitespace {
                let text = LeaText.clean(current)
                if !text.isEmpty { out.append((text, moves)); moves = [] }
                current = ""
            }
            i = next
        }
        let rest = LeaText.clean(current)
        if !rest.isEmpty { out.append((rest, moves)) } else if !moves.isEmpty, !out.isEmpty { out[out.count - 1].1 += moves }
        let photo = points.first?["photo"] as? String
        return Turn(question: question, sentences: out.map { (text: $0.0, moves: $0.1) }, photo: photo)
    }

    /// The cited still, saved in the session copy's crops under its SHA-256 (as the gateway names crops).
    private func savePhoto(_ still: String?) -> String? {
        guard let still, let data = try? Data(contentsOf: sessionDir.appending(path: "stills/\(still).jpg")) else { return nil }
        let sha = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        let dir = CropSource.dir(sessionDir)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appending(path: sha)
        if !FileManager.default.fileExists(atPath: file.path) { try? data.write(to: file) }
        return sha
    }

    /// The proof crop for a move with Lea's own pointer in a photo: the full-resolution still, upright, cropped round
    /// her point (15% of the shorter side, so the part fills about half the card) with the ring drawn on it, as the
    /// gateway's spot photo looks. Saved by its SHA-256 in the session copy's crops.
    private func spotCrop(_ m: Move) -> String? {
        guard let px = m.pixel,
              let name = try? FileManager.default.contentsOfDirectory(atPath: sessionDir.appending(path: "stills").path)
                .first(where: { $0.hasSuffix("_\(px.photo).jpg") }),
              let still = UIImage(contentsOfFile: sessionDir.appending(path: "stills/\(name)").path), let cg = still.cgImage else { return nil }
        let upright = cg.width > cg.height ? UIImage(cgImage: cg, scale: 1, orientation: .right) : still
        let size = upright.size
        let side = min(size.width, size.height) * 0.15
        let centre = CGPoint(x: px.x * size.width, y: px.y * size.height)
        let window = CGRect(x: centre.x - side / 2, y: centre.y - side / 2, width: side, height: side)
        let out = CGSize(width: 600, height: 600)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let image = UIGraphicsImageRenderer(size: out, format: format).image { ctx in
            let k = out.width / side
            upright.draw(in: CGRect(x: -window.minX * k, y: -window.minY * k, width: size.width * k, height: size.height * k))
            let ring = CGRect(x: out.width * 0.2, y: out.height * 0.2, width: out.width * 0.6, height: out.height * 0.6)
            ctx.cgContext.setStrokeColor(UIColor(hex: FixedHex.keyline, alpha: 0.8).cgColor)
            ctx.cgContext.setLineWidth(14)
            ctx.cgContext.strokeEllipse(in: ring)
            ctx.cgContext.setStrokeColor(UIColor(hex: FixedHex.next).cgColor)
            ctx.cgContext.setLineWidth(8)
            ctx.cgContext.strokeEllipse(in: ring)
        }
        guard let data = image.jpegData(compressionQuality: 0.9) else { return nil }
        let sha = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        let dir = CropSource.dir(sessionDir)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? data.write(to: dir.appending(path: sha))
        return sha
    }

    private func play(_ t: Turn) async {
        try? await Task.sleep(for: .seconds(2))
        feed.replay(question: t.question, sentence: nil, thinking: true, speaking: false)
        try? await Task.sleep(for: .seconds(1.4))
        let sha = savePhoto(t.photo)
        var said: [String] = []
        var photoFor: [String: String] = [:]
        var n = 0
        for s in t.sentences {
            for m in s.moves { if let crop = spotCrop(m) ?? sha { photoFor[m.id] = crop } }
            said.append(s.text)
            feed.replay(question: t.question, sentence: s.text, thinking: false, speaking: true, photoFor: photoFor,
                        answer: said)
            for m in s.moves {
                n += 1
                cue = LeaStageCue(n: n, command: .point(id: m.id, at: m.point, label: m.label, radius: 0.012))
                if s.moves.count > 1 { try? await Task.sleep(for: .seconds(Double(s.text.count) / 14 / Double(s.moves.count))) }
            }
            try? await Task.sleep(for: .seconds(max(1.2, Double(s.text.count) / 14)))
        }
        feed.replay(question: t.question, sentence: t.sentences.last?.text, thinking: false, speaking: false,
                    photoFor: photoFor, answer: said)
    }
}

/// `--ui-walkthrough`: bumps the stage's walkthrough trigger once, as Lea's voice action would.
private struct GalleryWalkthrough<Content: View>: View {
    @ViewBuilder let content: (Int) -> Content
    @State private var walk = 0
    var body: some View {
        content(walk).task {
            guard CommandLine.arguments.contains("--ui-walkthrough") else { return }
            try? await Task.sleep(for: .seconds(2))
            walk += 1
        }
    }
}

#Preview("Progress") {
    ReportProgressView(progress: PipelineProgress(step: .upload), uploaded: 12, pending: 18, error: nil, retry: {})
}
#endif

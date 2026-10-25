import ImageIO
import SwiftUI

/// Removing scans from the phone, through `ScanStorage` (which also cancels queued uploads and never touches the
/// session being captured). Only the phone copy is removed, never the Mac's.
@MainActor
enum ScanLibrary {
    static func canDelete(_ record: SessionRecord, app: AppState) -> Bool { app.storage.canDelete(record) }

    static func delete(_ record: SessionRecord, app: AppState) {
        app.storage.delete(record)
        app.reloadSessions()
    }

    /// How many scans "Delete all" would remove (all but one being captured).
    static func deletableCount(app: AppState) -> Int { app.sessions.filter(app.storage.canDelete).count }

    static func deleteAll(app: AppState) {
        app.storage.deleteAll()
        app.reloadSessions()
    }

    /// Bytes the scans use on disk, measured off the main actor.
    @concurrent
    static func usedBytes(_ storage: ScanStorage) async -> Int64 { storage.usedBytes() }

    static func size(_ bytes: Int64) -> String { bytes.formatted(.byteCount(style: .file)) }
}

/// The report JSON kept next to a session once it arrives, so the list can show the verdict and the report
/// reopens without the network.
enum CachedReport {
    static func url(_ record: SessionRecord, storage: ScanStorage) -> URL { storage.dir(record).appending(path: "report.json") }

    static func save(_ json: [String: Any], record: SessionRecord, storage: ScanStorage) {
        guard let data = try? JSONSerialization.data(withJSONObject: json) else { return }
        try? data.write(to: url(record, storage: storage), options: .atomic)
    }

    static func load(_ record: SessionRecord, storage: ScanStorage) -> [String: Any]? {
        (try? Data(contentsOf: url(record, storage: storage))).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
    }
}

// MARK: - List

/// Every scan on the phone, newest first. Tap opens it; swipe or long-press to delete (with a confirmation).
struct ScansView: View {
    @Environment(AppState.self) private var app
    let open: (SessionRecord) -> Void
    @State private var pendingDelete: SessionRecord?

    var body: some View {
        List {
            ForEach(app.sessions) { s in
                ScanRowButton(record: s, open: open, delete: { pendingDelete = s })
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Palette.canvas.ignoresSafeArea())
        .overlay {
            if app.sessions.isEmpty {
                ContentUnavailableView("No scans", systemImage: Symbol.scan, description: Text("Scans you take show up here."))
            }
        }
        .navigationTitle("Scans")
        .navigationBarTitleDisplayMode(.large)
        .confirmDeleteScan($pendingDelete)
    }
}

/// One scan row as a list row: tap opens, swipe and long-press delete.
struct ScanRowButton: View {
    let record: SessionRecord
    let open: (SessionRecord) -> Void
    let delete: () -> Void
    @Environment(AppState.self) private var app

    var body: some View {
        let deletable = ScanLibrary.canDelete(record, app: app)
        Button { open(record) } label: { SessionRow(record: record) }
            .buttonStyle(PressableCardStyle())
            .listRowInsets(EdgeInsets(top: Spacing.xs, leading: Spacing.gutter, bottom: Spacing.xs, trailing: Spacing.gutter))
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            .swipeActions { if deletable { Button("Delete", systemImage: "trash", role: .destructive, action: delete) } }
            .contextMenu { Button("Delete scan", systemImage: "trash", role: .destructive, action: delete).disabled(!deletable) }
    }
}

extension View {
    /// Asks before deleting one scan; deletes only the phone copy.
    func confirmDeleteScan(_ record: Binding<SessionRecord?>, deleted: @escaping () -> Void = {}) -> some View {
        modifier(ConfirmDeleteScan(record: record, deleted: deleted))
    }
}

private struct ConfirmDeleteScan: ViewModifier {
    @Environment(AppState.self) private var app
    @Binding var record: SessionRecord?
    let deleted: () -> Void

    func body(content: Content) -> some View {
        content.confirmationDialog("Delete this scan?", isPresented: Binding(get: { record != nil }, set: { if !$0 { record = nil } }),
                                   titleVisibility: .visible, presenting: record) { r in
            Button("Delete scan", role: .destructive) {
                guard ScanLibrary.canDelete(r, app: app) else { record = nil; return }
                ScanLibrary.delete(r, app: app)
                record = nil
                deleted()
            }
            Button("Cancel", role: .cancel) { record = nil }
        } message: { r in
            Text("\(r.stillIDs.count) photos and the report are removed from this phone. The copy on the server stays.")
        }
    }
}

// MARK: - Row

struct SessionRow: View {
    let record: SessionRecord
    @Environment(AppState.self) private var app
    @State private var summary: Summary?

    struct Summary: Sendable { var verdict: ReportModel.Verdict?; var thumb: UIImage? }

    var body: some View {
        HStack(spacing: Spacing.m) {
            ZStack {
                Palette.well
                if let thumb = summary?.thumb {
                    Image(uiImage: thumb).resizable().scaledToFill().transition(.opacity)
                } else {
                    Image(systemName: look.symbol).foregroundStyle(look.tone.color)
                }
            }
            .frame(width: 52, height: 52)
            .clipShape(RoundedRectangle(cornerRadius: Radius.thumb, style: .continuous))
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(record.symptom ?? "Scan").font(.body.weight(.semibold)).foregroundStyle(.primary).lineLimit(2)
                Text("\(record.createdAt.formatted(.dateTime.month(.abbreviated).day().hour().minute())) · \(record.stillIDs.count) photos")
                    .font(.footnote).foregroundStyle(.secondary)
                // A reported scan shows its verdict in the verdict's colour (amber for problems, never green);
                // anything else shows where it is on the way to a report.
                let status: (words: String, tone: Tone) = summary?.verdict.map { ($0.words, verdictTone($0)) } ?? (look.words, look.tone)
                HStack(spacing: 5) {
                    Circle().fill(status.tone.color).frame(width: 7, height: 7)
                    Text(status.words)
                }
                .font(.footnote.weight(.semibold))
                .foregroundStyle(status.tone == .neutral ? Color.secondary : status.tone.text)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .card(padding: Spacing.m)
        .accessibilityElement(children: .combine)
        .task(id: record.status) {
            let loaded = await Self.summary(dir: app.storage.dir(record), stillIDs: record.stillIDs, scale: 3)
            withAnimation(Motion.fade) { summary = loaded }
        }
    }

    private func verdictTone(_ v: ReportModel.Verdict) -> Tone {
        switch v {
        case .potentialIssues: .caution
        case .looksGood, .noProblemsSeen: .good
        default: .neutral
        }
    }

    private var look: (symbol: String, words: String, tone: Tone) {
        switch record.status {
        case .capturing: ("record.circle", "Not finished", .caution)
        case .captured: ("tray.full.fill", "Captured", .neutral)
        case .uploading: ("arrow.up.circle.fill", "Uploading", .accent)
        case .uploaded: ("hourglass", "Analysing", .accent)
        case .reported: ("doc.text.fill", "Report ready", .good)
        case .failed: ("exclamationmark.circle.fill", "Failed", .danger)
        }
    }

    /// The cached verdict and a 52 pt thumbnail of the first local photo, read off the main actor.
    @concurrent
    private static func summary(dir: URL, stillIDs: [String], scale: CGFloat) async -> Summary {
        let fm = FileManager.default
        let report = (try? Data(contentsOf: dir.appending(path: "report.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        var candidates = stillIDs.prefix(1).map { dir.appending(path: "stills/\($0).jpg") }
        let images = dir.appending(path: "oc/Images")
        if let first = (try? fm.contentsOfDirectory(atPath: images.path))?.filter({ $0.lowercased().hasSuffix(".heic") }).sorted().first {
            candidates.append(images.appending(path: first))
        }
        var thumb: UIImage?
        if let url = candidates.first(where: { fm.fileExists(atPath: $0.path) }), let src = CGImageSourceCreateWithURL(url as CFURL, nil) {
            let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                            kCGImageSourceCreateThumbnailWithTransform: true,
                                            kCGImageSourceThumbnailMaxPixelSize: Int(52 * scale)]
            thumb = CGImageSourceCreateThumbnailAtIndex(src, 0, options as CFDictionary).map { UIImage(cgImage: $0) }
        }
        return Summary(verdict: (report?["verdict"] as? String).map(ReportModel.Verdict.init), thumb: thumb)
    }
}

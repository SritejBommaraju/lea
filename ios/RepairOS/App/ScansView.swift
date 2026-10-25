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

// MARK: - Rows

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
            .listRowInsets(EdgeInsets(top: Spacing.m, leading: Spacing.gutter, bottom: Spacing.m, trailing: Spacing.gutter))
            // Rows are separated by hairlines, not boxed one by one.
            .listRowSeparator(.hidden, edges: .top)
            .listRowSeparator(.visible, edges: .bottom)
            .listRowSeparatorTint(Palette.hairline)
            .alignmentGuide(.listRowSeparatorLeading) { _ in 0 }
            .listRowBackground(Color.clear)
            // Not `role: .destructive`: that removes the row optimistically while the confirmation is still open, and
            // the real delete then leaves the list's item count out of step (UICollectionView invalid-update crash).
            .swipeActions { if deletable { Button("Delete", systemImage: "trash", action: delete).tint(Palette.danger) } }
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
                record = nil
                guard ScanLibrary.canDelete(r, app: app) else { return }
                // After the dialog has gone, so the list applies one update, not one mid-dismissal.
                Task { @MainActor in
                    withAnimation { ScanLibrary.delete(r, app: app) }
                    deleted()
                }
            }
            Button("Cancel", role: .cancel) { record = nil }
        } message: { r in
            Text("\(r.stillIDs.count) photos and the report are removed from this phone. The copy on the server stays.")
        }
    }
}

// MARK: - Row

/// One scan: a 48 pt thumbnail, the person's own words as the title ("Scan" when they gave none), the date, time and
/// photo count, and a read-only status tag. A reported scan shows its verdict ("Safety problem" when a most likely
/// problem is a safety one); anything else shows where it is on the way to a report, never in violet.
struct SessionRow: View {
    let record: SessionRecord
    @Environment(AppState.self) private var app
    @State private var summary: Summary?

    struct Summary: Sendable { var verdict: ReportModel.Verdict?; var safety = false; var thumb: UIImage? }

    var body: some View {
        HStack(spacing: Spacing.m) {
            ZStack {
                Palette.well
                if let thumb = summary?.thumb {
                    Image(uiImage: thumb).resizable().scaledToFill().transition(.opacity)
                } else {
                    Image(systemName: Symbol.photo).foregroundStyle(Palette.secondary)
                }
            }
            .frame(width: 48, height: 48)
            .clipShape(RoundedRectangle(cornerRadius: Radius.thumb, style: .continuous))
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(record.symptom ?? "Scan").font(.body.weight(.semibold)).foregroundStyle(Palette.ink).lineLimit(2)
                    .multilineTextAlignment(.leading)
                Text(meta).font(.footnote).foregroundStyle(Palette.secondary)
                tag
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Palette.secondary)
                .accessibilityHidden(true)
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .task(id: record.status) {
            let loaded = await Self.summary(dir: app.storage.dir(record), stillIDs: record.stillIDs, scale: 3)
            withAnimation(Motion.fade) { summary = loaded }
        }
    }

    /// "24 Oct · 14:32 · 12 photos".
    private var meta: String {
        let day = record.createdAt.formatted(.dateTime.day().month(.abbreviated))
        let time = record.createdAt.formatted(.dateTime.hour().minute())
        let photos = record.stillIDs.count == 1 ? "1 photo" : "\(record.stillIDs.count) photos"
        return "\(day) · \(time) · \(photos)"
    }

    @ViewBuilder private var tag: some View {
        if let verdict = summary?.verdict {
            switch verdict {
            case .potentialIssues where summary?.safety == true:
                ToneTag(text: "Safety problem", tone: .danger, symbol: Symbol.safety)
            case .potentialIssues:
                ToneTag(text: verdict.words, tone: .caution, symbol: "exclamationmark.circle")
            case .insufficientEvidence:
                ToneTag(text: "Needs a closer look", tone: .neutral, dashed: true)
            default:
                ToneTag(text: verdict.words, tone: .neutral)
            }
        } else {
            switch record.status {
            case .capturing: ToneTag(text: "Not finished", tone: .neutral)
            case .captured: ToneTag(text: "Captured", tone: .neutral)
            case .uploading: ToneTag(text: "Uploading", tone: .neutral)
            case .uploaded: ToneTag(text: "Analysing", tone: .neutral)
            case .reported: ToneTag(text: "Report ready", tone: .neutral)
            case .failed: ToneTag(text: "Failed", tone: .danger, symbol: "exclamationmark.circle")
            }
        }
    }

    /// The cached verdict (and whether a most likely problem is a safety one) and a 48 pt thumbnail of the first local photo, read off the main actor.
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
                                            kCGImageSourceThumbnailMaxPixelSize: Int(48 * scale)]
            thumb = CGImageSourceCreateThumbnailAtIndex(src, 0, options as CFDictionary).map { UIImage(cgImage: $0) }
        }
        let model = report.map(ReportModel.init(json:))
        return Summary(verdict: model?.verdict, safety: model?.mostLikely.contains { $0.severity == .safety } ?? false, thumb: thumb)
    }
}

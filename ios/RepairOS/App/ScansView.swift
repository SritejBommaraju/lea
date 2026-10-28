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

    /// Deletes several scans in one go (Home's selection mode): each folder once, never the one being captured, then
    /// the list reloads once (not once per scan, so the List applies a single update). Returns how many went.
    @discardableResult
    static func delete(_ records: [SessionRecord], app: AppState) -> Int {
        delete(records, storage: app.storage, reload: app.reloadSessions)
    }

    @discardableResult
    static func delete(_ records: [SessionRecord], storage: ScanStorage, reload: () -> Void) -> Int {
        let n = storage.delete(records)
        reload()
        return n
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

/// One scan row as a list row: tap opens, swipe and long-press delete; long-press → Select starts selection mode. In
/// selection mode a tap ticks it (a checkbox at the leading edge) and there is no swipe.
struct ScanRowButton: View {
    let record: SessionRecord
    let open: (SessionRecord) -> Void
    let delete: () -> Void
    var selecting = false
    var selected = false
    var toggle: () -> Void = {}
    var select: () -> Void = {}
    /// Hands the scan's folder to someone who can look at it (the system share sheet).
    var share: () -> Void = {}
    @EnvironmentObject private var app: AppState

    var body: some View {
        let deletable = ScanLibrary.canDelete(record, app: app)
        Button { if selecting { toggle() } else { open(record) } } label: {
            HStack(spacing: Spacing.m) {
                if selecting {
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .font(.title3)
                        .foregroundStyle(selected ? Palette.accent : Palette.secondary)
                        .opacity(deletable ? 1 : 0.35)
                        .symbolReplaceTransition()
                        .transition(.move(edge: .leading).combined(with: .opacity))
                        .accessibilityHidden(true)
                }
                SessionRow(record: record)
            }
        }
            .buttonStyle(PressableCardStyle())
            .disabled(selecting && !deletable)
            .accessibilityAddTraits(selecting && selected ? .isSelected : [])
            .accessibilityHint(selecting ? (deletable ? "Selects it for deleting" : "Being captured: can't be deleted") : "")
            .listRowInsets(EdgeInsets(top: Spacing.m, leading: Spacing.gutter, bottom: Spacing.m, trailing: Spacing.gutter))
            // Rows are separated by hairlines, not boxed one by one.
            .listRowSeparator(.hidden, edges: .top)
            .listRowSeparator(.visible, edges: .bottom)
            .listRowSeparatorTint(Palette.hairline)
            .alignmentGuide(.listRowSeparatorLeading) { _ in 0 }
            .listRowBackground(Color.clear)
            // Not `role: .destructive`: that removes the row optimistically while the confirmation is still open, and
            // the real delete then leaves the list's item count out of step (UICollectionView invalid-update crash).
            .swipeActions { if deletable, !selecting { Button("Delete", systemImage: "trash", action: delete).tint(Palette.danger) } }
            .contextMenu {
                if !selecting {
                    Button("Select", systemImage: "checkmark.circle", action: select).disabled(!deletable)
                    Button("Share scan files", systemImage: "square.and.arrow.up", action: share)
                    Button("Delete scan", systemImage: "trash", role: .destructive, action: delete).disabled(!deletable)
                }
            }
    }
}

extension View {
    /// Asks before deleting one scan; deletes only the phone copy.
    func confirmDeleteScan(_ record: Binding<SessionRecord?>, deleted: @escaping () -> Void = {}) -> some View {
        modifier(ConfirmDeleteScan(record: record, deleted: deleted))
    }
}

private struct ConfirmDeleteScan: ViewModifier {
    @EnvironmentObject private var app: AppState
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
/// problem is a safety one; never a "closer look" prompt); anything else shows where it is on the way to a report,
/// never in the accent.
struct SessionRow: View {
    let record: SessionRecord
    @EnvironmentObject private var app: AppState
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var summary: Summary?

    struct Summary: Sendable { var verdict: ReportModel.Verdict?; var safety = false; var thumb: UIImage? }

    var body: some View {
        HStack(spacing: Spacing.m) {
            // At accessibility text sizes the words need the width more than the thumbnail does.
            if !typeSize.isAccessibilitySize {
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
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(record.symptom ?? "Scan").font(.body.weight(.semibold)).foregroundStyle(Palette.ink).lineLimit(typeSize.isAccessibilitySize ? 5 : 2)
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
            default:
                ToneTag(text: verdict.words, tone: .neutral)
            }
        } else {
            switch record.status {
            case .capturing: ToneTag(text: "Not finished", tone: .neutral)
            case .captured: ToneTag(text: "Saved on this phone", tone: .neutral)
            case .uploading: ToneTag(text: "Sending to the server", tone: .neutral)
            case .uploaded: ToneTag(text: "Being checked", tone: .neutral)
            case .reported: ToneTag(text: "Report ready", tone: .neutral)
            case .failed: ToneTag(text: "Stopped. Open it to try again", tone: .caution, symbol: "exclamationmark.circle")
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

// MARK: - Selecting several scans

/// Home's selection mode (Select, or long-press → Select): which scans are ticked. Only scans that can be deleted can
/// be ticked (never the one being captured: `ScanLibrary.canDelete`). Kept apart from the view so it is tested.
struct ScanSelection: Equatable, Sendable {
    private(set) var active = false
    private(set) var ids: Set<String> = []

    var count: Int { ids.count }
    func contains(_ id: String) -> Bool { ids.contains(id) }

    /// Selection mode on, with `first` ticked when it can be (long-press → Select on that row).
    mutating func begin(with first: String? = nil, selectable: Set<String>) {
        active = true
        ids = first.map { selectable.contains($0) ? [$0] : [] } ?? []
    }

    mutating func toggle(_ id: String, selectable: Set<String>) {
        guard active, selectable.contains(id) else { return }
        if ids.contains(id) { ids.remove(id) } else { ids.insert(id) }
    }

    func allSelected(_ selectable: Set<String>) -> Bool { !selectable.isEmpty && selectable.isSubset(of: ids) }

    /// Select all, or deselect all when everything is already ticked.
    mutating func toggleAll(_ selectable: Set<String>) {
        guard active else { return }
        ids = allSelected(selectable) ? [] : selectable
    }

    /// The list changed (a reload, a delete, a capture starting): ticks on scans that are gone or no longer
    /// deletable are dropped.
    mutating func prune(to selectable: Set<String>) { ids.formIntersection(selectable) }

    mutating func end() {
        active = false
        ids = []
    }

    /// "Delete 1 scan", "Delete 3 scans".
    static func deleteTitle(_ n: Int) -> String { n == 1 ? "Delete 1 scan" : "Delete \(n) scans" }
    /// The one confirmation's title.
    static func confirmTitle(_ n: Int) -> String { (n == 1 ? "Delete 1 scan" : "Delete \(n) scans") + "?" }
    static let confirmMessage = "Photos and reports are removed from this phone. Copies on the server stay."
}

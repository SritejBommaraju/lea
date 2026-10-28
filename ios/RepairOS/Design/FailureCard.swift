import SwiftUI
import UIKit

/// A failure in plain words with its one next step as a button (`Failure`). Opening the phone's Settings for lea is done
/// here; every other action is the caller's, since only the caller knows what "try again" means on its screen.
///
/// Contract: `failure` is what to say; `perform` runs the action the person tapped, except `.openSettings`.
struct FailureCard: View {
    let failure: Failure
    var perform: (Failure.Action) -> Void = { _ in }
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Image(systemName: failure.symbol).foregroundStyle(Palette.caution).accessibilityHidden(true)
                Text(failure.title).font(.body.weight(.semibold)).foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
            }
            Text(failure.message).font(.subheadline).foregroundStyle(Palette.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let action = failure.action {
                Button(action.label) { run(action) }
                    .buttonStyle(.primary(.quiet))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .card(tone: .caution)
        .accessibilityElement(children: .contain)
    }

    private func run(_ action: Failure.Action) {
        if action == .openSettings {
            if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
        } else {
            perform(action)
        }
    }
}

/// The system share sheet for files (UIActivityViewController). Used to hand a scan's folder to someone who can look at it.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    /// Called when the sheet finishes or is cancelled.
    var finished: () -> Void = {}

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: items, applicationActivities: nil)
        let done = finished
        controller.completionWithItemsHandler = { _, _, _, _ in done() }
        return controller
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

/// A scan's folder as one zip file, made by the system (`NSFileCoordinator` with `.forUploading`) in the temporary folder.
enum ScanExport {
    /// Every zip is made here, so they can all be removed together.
    static var folder: URL { FileManager.default.temporaryDirectory.appending(path: "lea-shares", directoryHint: .isDirectory) }

    /// Removes the zips left behind (after a share, and at launch).
    static func clear() { try? FileManager.default.removeItem(at: folder) }

    /// The zip, or nil when it can't be made (no space, the folder is gone). Off the main actor: a scan can be large.
    @concurrent
    static func zip(_ folder: URL, name: String) async -> URL? {
        var coordinationError: NSError?
        var result: URL?
        NSFileCoordinator().coordinate(readingItemAt: folder, options: [.forUploading], error: &coordinationError) { zipped in
            try? FileManager.default.removeItem(at: Self.folder)
            try? FileManager.default.createDirectory(at: Self.folder, withIntermediateDirectories: true)
            let destination = Self.folder.appending(path: "\(name).zip")
            if (try? FileManager.default.copyItem(at: zipped, to: destination)) != nil { result = destination }
        }
        return result
    }
}

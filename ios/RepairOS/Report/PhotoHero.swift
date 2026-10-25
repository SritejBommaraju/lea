import ImageIO
import SwiftUI
import UIKit

/// The hero when the backend says photos read better than 3D (an object too small for LiDAR): the best photo, upright,
/// with each numbered finding's crop box drawn where it is, and a strip of the other photos. Everything comes from
/// the scan's own stills on the phone (`stills/<still_id>.jpg`, sensor orientation; `box_px` in its pixels).
struct PhotoHero: View {
    struct Mark: Identifiable, Equatable, Sendable {
        var id: String
        var number: Int
        var still: String
        /// [x0, y0, x1, y1] in the full still's pixels (sensor orientation), when given.
        var box: [Double]?
        var tone: Tone
    }

    let stillsDir: URL
    let numbered: [Mark]
    var focus: String?
    let onMarkTap: (String) -> Void

    @State private var lead: String?
    @State private var image: StillImage?
    /// Every still on the phone (largest first), for a report with nothing numbered on a photo (nothing looked wrong).
    @State private var available: [String] = []
    @State private var listed = false
    @State private var loadFailed = false

    private var stills: [String] { Self.heroStills(marked: numbered, available: available) }
    private var shown: String? { lead ?? stills.first }

    /// The photos the hero shows: the numbered findings' stills in marker order; with none, the scan's own stills (so
    /// the hero always has a picture when the phone has one, and never waits on a finding).
    nonisolated static func heroStills(marked: [Mark], available: [String]) -> [String] {
        var out: [String] = []
        for m in marked where !out.contains(m.still) { out.append(m.still) }
        return out.isEmpty ? Array(available.prefix(12)) : out
    }

    /// The still ids in `dir` (`<id>.jpg`), largest file first (the most detail).
    @concurrent
    static func stillIDs(in dir: URL) async -> [String] {
        let files = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        var sized: [(id: String, bytes: Int)] = []
        for url in files where url.pathExtension.lowercased() == "jpg" {
            let bytes = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            sized.append((url.deletingPathExtension().lastPathComponent, bytes))
        }
        sized.sort { a, b in a.bytes != b.bytes ? a.bytes > b.bytes : a.id < b.id }
        return sized.map(\.id)
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Palette.stage
                if let image {
                    GeometryReader { geo in
                        let fit = image.fit(in: geo.size)
                        Image(uiImage: image.upright).resizable().interpolation(.high)
                            .frame(width: fit.width, height: fit.height)
                            .position(x: geo.size.width / 2, y: geo.size.height / 2)
                        ForEach(numbered.filter { $0.still == shown }) { m in
                            if let r = image.uprightRect(m.box, fit: fit, in: geo.size) {
                                MarkBox(mark: m, selected: focus == m.id)
                                    .frame(width: max(28, r.width), height: max(28, r.height))
                                    .position(x: r.midX, y: r.midY)
                                    .onTapGesture { onMarkTap(m.id) }
                            }
                        }
                    }
                    .transition(.opacity)
                } else if loadFailed || (listed && shown == nil) {
                    // Every loading state ends: no photo on the phone (or it can't be read) says so instead of spinning.
                    Label(shown == nil ? "No photos of this scan on the phone" : "This photo can't be opened",
                          systemImage: Symbol.photo)
                        .font(.footnote).foregroundStyle(Palette.chalk.opacity(0.7))
                } else {
                    ProgressView().tint(Palette.chalk)
                }
            }
            .frame(height: 340)
            .clipped()
            if stills.count > 1 { strip }
        }
        .task(id: stillsDir) {
            available = await Self.stillIDs(in: stillsDir)
            listed = true
        }
        .task(id: shown) {
            guard let shown else { return }
            loadFailed = false
            let url = stillsDir.appending(path: "\(shown).jpg")
            let loaded = await StillImage.load(url, maxPixels: 1600)
            loadFailed = loaded == nil
            withAnimation(Motion.fade) { image = loaded }
        }
        .onChange(of: focus) { _, id in
            if let still = numbered.first(where: { $0.id == id })?.still, still != shown { lead = still }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Photo of the object with \(numbered.count) numbered problems")
    }

    private var strip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Spacing.s) {
                ForEach(stills, id: \.self) { s in
                    Button { lead = s } label: {
                        StillThumb(url: stillsDir.appending(path: "\(s).jpg"))
                            .frame(width: 56, height: 56)
                            .clipShape(RoundedRectangle(cornerRadius: Radius.small, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: Radius.small, style: .continuous)
                                .strokeBorder(s == shown ? Palette.accent : Palette.hairline, lineWidth: s == shown ? 2 : Line.hairline))
                            .overlay(alignment: .topLeading) {
                                if let m = numbered.first(where: { $0.still == s }) {
                                    Marker(number: m.number, tone: m.tone, size: 18, onStage: true).padding(3)
                                }
                            }
                    }
                    .buttonStyle(PressableCardStyle())
                    .accessibilityLabel("Photo \(stills.firstIndex(of: s).map { $0 + 1 } ?? 0)")
                }
            }
            .padding(.horizontal, Spacing.m).padding(.vertical, Spacing.s)
        }
    }
}

/// The numbered finding on the photo: a tone corner set around its box (the lock-bracket shape, over a keyline) with
/// its `Marker` at the corner. Selected: the heavier locked stroke. The photo itself is never tinted.
private struct MarkBox: View {
    let mark: PhotoHero.Mark
    let selected: Bool

    var body: some View {
        Color.clear
            .keylined(CornerBrackets(), color: mark.tone.cameraColor, width: selected ? Line.locked : Line.drawing)
            .overlay(alignment: .topLeading) {
                Marker(number: mark.number, tone: mark.tone, onStage: true).offset(x: -13, y: -13)
            }
            .contentShape(Rectangle())
            .motion(Motion.fade, value: selected)
            .accessibilityElement()
            .accessibilityLabel("Problem \(mark.number)")
            .accessibilityAddTraits(.isButton)
    }
}

/// A still decoded for display: upright (sensor images are landscape; the phone was upright), with the scale from the
/// full still's pixels.
struct StillImage: Sendable {
    let upright: UIImage
    /// Full still size in sensor orientation (pixels).
    let full: CGSize

    /// Upright size (width and height swapped).
    var uprightSize: CGSize { CGSize(width: full.height, height: full.width) }

    func fit(in size: CGSize) -> CGSize {
        let s = min(size.width / uprightSize.width, size.height / uprightSize.height)
        return CGSize(width: uprightSize.width * s, height: uprightSize.height * s)
    }

    /// `box` [x0, y0, x1, y1] (sensor pixels) → the view rect, after the 90° clockwise turn to upright
    /// (x' = H − y, y' = x).
    func uprightRect(_ box: [Double]?, fit: CGSize, in view: CGSize) -> CGRect? {
        guard let b = box, b.count == 4, full.width > 0 else { return nil }
        let s = fit.width / uprightSize.width
        let ox = (view.width - fit.width) / 2, oy = (view.height - fit.height) / 2
        let x0 = (full.height - b[3]) * s + ox, x1 = (full.height - b[1]) * s + ox
        let y0 = b[0] * s + oy, y1 = b[2] * s + oy
        return CGRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
    }

    @concurrent
    static func load(_ url: URL, maxPixels: Int) async -> StillImage? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
              let w = props[kCGImagePropertyPixelWidth] as? Int, let h = props[kCGImagePropertyPixelHeight] as? Int else { return nil }
        let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                        kCGImageSourceCreateThumbnailWithTransform: false,
                                        kCGImageSourceThumbnailMaxPixelSize: maxPixels]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(src, 0, options as CFDictionary) else { return nil }
        // Draw it turned upright, so SwiftUI lays it out by its real (portrait) size.
        let up = CGSize(width: cg.height, height: cg.width)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let upright = UIGraphicsImageRenderer(size: up, format: format).image { _ in
            UIImage(cgImage: cg, scale: 1, orientation: .right).draw(in: CGRect(origin: .zero, size: up))
        }
        return StillImage(upright: upright, full: CGSize(width: w, height: h))
    }
}

/// A small square thumbnail of a still (upright, centre-cropped).
private struct StillThumb: View {
    let url: URL
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            Palette.well
            if let image { Image(uiImage: image).resizable().scaledToFill() }
        }
        .task(id: url) { image = await StillImage.load(url, maxPixels: 200)?.upright }
    }
}

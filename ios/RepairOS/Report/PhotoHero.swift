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

    private var stills: [String] {
        var out: [String] = []
        for m in numbered where !out.contains(m.still) { out.append(m.still) }
        return out
    }
    private var shown: String? { lead ?? stills.first }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Palette.well
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
                } else {
                    ProgressView()
                }
            }
            .frame(height: 340)
            .clipped()
            if stills.count > 1 { strip }
        }
        .task(id: shown) {
            guard let shown else { return }
            let url = stillsDir.appending(path: "\(shown).jpg")
            let loaded = await StillImage.load(url, maxPixels: 1600)
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
                                .strokeBorder(s == shown ? Palette.accent : Palette.hairline, lineWidth: s == shown ? 2 : 0.5))
                            .overlay(alignment: .topLeading) {
                                if let n = numbered.first(where: { $0.still == s })?.number {
                                    Text("\(n)").font(.system(.caption2, design: .rounded, weight: .bold))
                                        .foregroundStyle(.white).frame(width: 18, height: 18)
                                        .background(Palette.danger, in: Circle()).padding(3)
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

/// The numbered finding on the photo: its crop box outline with the number at the corner.
private struct MarkBox: View {
    let mark: PhotoHero.Mark
    let selected: Bool

    var body: some View {
        RoundedRectangle(cornerRadius: 8, style: .continuous)
            .strokeBorder(mark.tone.color, lineWidth: selected ? 3 : 2)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(mark.tone.color.opacity(selected ? 0.12 : 0.04)))
            .overlay(alignment: .topLeading) {
                Text("\(mark.number)")
                    .font(.system(.footnote, design: .rounded, weight: .bold)).monospacedDigit()
                    .foregroundStyle(.white)
                    .frame(width: 26, height: 26)
                    .background(mark.tone.color, in: Circle())
                    .overlay(Circle().strokeBorder(.white, lineWidth: 2))
                    .offset(x: -11, y: -11)
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

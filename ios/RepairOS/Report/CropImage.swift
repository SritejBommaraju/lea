import ImageIO
import SwiftUI
import UIKit

/// Where crop images come from: the paired gateway (GET /v1/blobs/<sha256>, the device token) or, in DEBUG
/// previews, a local folder laid out like the gateway's blob store (`<root>/<aa>/<sha256>`).
struct CropSource: Sendable {
    let fetch: @Sendable (String) async -> Data?

    static func gateway(_ gateway: GatewayClient) -> CropSource {
        CropSource { sha in
            var req = URLRequest(url: gateway.baseURL.appending(path: "v1/blobs/\(sha)"))
            if let token = gateway.token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
            guard let (data, resp) = try? await URLSession.shared.data(for: req),
                  (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            return data
        }
    }

    static func folder(_ root: URL) -> CropSource {
        CropSource { sha in try? Data(contentsOf: root.appending(path: String(sha.prefix(2))).appending(path: sha)) }
    }

    static let none = CropSource { _ in nil }

    /// `<session>/crops/`: crop blobs kept on the phone, so a report (and Demo replay) shows its photos offline.
    static func dir(_ sessionDir: URL) -> URL { sessionDir.appending(path: "crops", directoryHint: .isDirectory) }

    /// The saved crop when there is one; else `fallback`, saving what it returns.
    static func saved(in dir: URL, fallback: CropSource?) -> CropSource {
        CropSource { sha in
            let file = dir.appending(path: sha)
            if let data = try? Data(contentsOf: file) { return data }
            guard let fallback, let data = await fallback.fetch(sha) else { return nil }
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? data.write(to: file, options: .atomic)
            return data
        }
    }
}

extension EnvironmentValues {
    @Entry var cropSource: CropSource = .none
}

/// Decoded thumbnails, keyed by sha and pixel size. Decoding happens off the main actor with ImageIO's
/// thumbnailer, so a 12 MP crop never decodes at full size for an 80 pt tile.
final class CropCache: @unchecked Sendable {
    static let shared = CropCache()
    private let cache = NSCache<NSString, UIImage>()

    init() { cache.countLimit = 120 }

    func cached(_ sha: String, pixels: Int) -> UIImage? { cache.object(forKey: "\(sha)@\(pixels)" as NSString) }

    func image(_ sha: String, pixels: Int, source: CropSource) async -> UIImage? {
        if let hit = cached(sha, pixels: pixels) { return hit }
        guard let data = await source.fetch(sha), let image = await Self.decode(data, pixels: pixels) else { return nil }
        cache.setObject(image, forKey: "\(sha)@\(pixels)" as NSString)
        return image
    }

    @concurrent
    private static func decode(_ data: Data, pixels: Int) async -> UIImage? {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: pixels,
        ]
        return CGImageSourceCreateThumbnailAtIndex(src, 0, options as CFDictionary).map { UIImage(cgImage: $0) }
    }
}

/// A crop tile: the image when it arrives (a short fade), a quiet well with a photo glyph until then.
struct CropThumb: View {
    let sha: String?
    var cornerRadius: CGFloat = Radius.thumb
    @Environment(\.cropSource) private var source
    @Environment(\.displayScale) private var scale
    @State private var image: UIImage?

    var body: some View {
        GeometryReader { geo in
            let pixels = Int(max(geo.size.width, geo.size.height) * scale)
            ZStack {
                Palette.well
                if let image {
                    Image(uiImage: image).resizable().scaledToFill()
                        .frame(width: geo.size.width, height: geo.size.height)
                        .transition(.opacity)
                } else {
                    Image(systemName: Symbol.photo).font(.title3).foregroundStyle(Palette.faint)
                }
            }
            .task(id: "\(sha ?? "")@\(pixels)") {
                guard let sha, pixels > 0 else { return }
                if let hit = CropCache.shared.cached(sha, pixels: pixels) { image = hit; return }
                let loaded = await CropCache.shared.image(sha, pixels: pixels, source: source)
                withAnimation(Motion.fade) { image = loaded }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous).strokeBorder(Palette.hairline, lineWidth: Line.hairline))
        .accessibilityHidden(true)
    }
}

/// The evidence photo full screen, decoded near full size: pinch or double-tap to zoom, drag to look around.
struct CropZoomView: View {
    let shas: [String]
    @State var index: Int
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            TabView(selection: $index) {
                ForEach(Array(shas.enumerated()), id: \.offset) { i, sha in
                    ZoomablePhoto(sha: sha).tag(i)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .ignoresSafeArea()
            if shas.count > 1 {
                Text("\(index + 1) of \(shas.count)").monospacedDigit().cameraChip()
                    .contentTransition(.numericText())
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Spacing.gutter)
                    .padding(.top, 6)
            }
            Button { dismiss() } label: {
                Image(systemName: Symbol.close)
            }
            .buttonStyle(StageIconButtonStyle())
            .padding(Spacing.gutter)
            .accessibilityLabel("Close")
        }
        .statusBarHidden()
    }
}

private struct ZoomablePhoto: View {
    let sha: String
    @Environment(\.cropSource) private var source
    @State private var image: UIImage?
    @State private var scale: CGFloat = 1
    @State private var lastScale: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero

    /// Enough pixels for a 3x zoom on a phone screen without decoding the full 12 MP still.
    static let pixels = 3000

    var body: some View {
        ZStack {
            if let image {
                Image(uiImage: image).resizable().scaledToFit()
                    .scaleEffect(scale).offset(offset)
                    .gesture(MagnifyGesture()
                        .onChanged { scale = min(6, max(1, lastScale * $0.magnification)) }
                        .onEnded { _ in lastScale = scale; if scale == 1 { reset() } })
                    .simultaneousGesture(DragGesture()
                        .onChanged { g in
                            guard scale > 1 else { return }
                            offset = CGSize(width: lastOffset.width + g.translation.width, height: lastOffset.height + g.translation.height)
                        }
                        .onEnded { _ in lastOffset = offset })
                    .onTapGesture(count: 2) {
                        withAnimation(Motion.standard) {
                            if scale > 1 { reset() } else { scale = 2.5; lastScale = 2.5 }
                        }
                    }
                    .accessibilityLabel("Evidence photo")
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task(id: sha) { image = await CropCache.shared.image(sha, pixels: Self.pixels, source: source) }
    }

    private func reset() { scale = 1; lastScale = 1; offset = .zero; lastOffset = .zero }
}

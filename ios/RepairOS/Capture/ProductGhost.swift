import SwiftUI
import UIKit

/// The product the customer is scanning, as a picture the app layer hands to the capture screen (a render of the design
/// the product was made from). It is optional: with none, the capture screen looks as before.
///
/// Hook for the App layer: `SessionFlowView(record:ghost:)` at the start, or `scan.ghost = ProductGhost(...)` later
/// when the picture arrives (the screen shows it as soon as it is set). This file only draws it; it never fetches
/// anything. The picture is 2D: it is a guide to what to look for, not a perspective-correct overlay.
struct ProductGhost: Equatable {
    var image: UIImage
    /// What to call it ("Desk lamp"), shown under the thumbnail when present.
    var name: String?

    init(image: UIImage, name: String? = nil) {
        self.image = image
        self.name = name
    }

    /// From a picture file on the phone (a cached render); nil when it cannot be read as an image.
    init?(contentsOf file: URL, name: String? = nil) {
        guard let data = try? Data(contentsOf: file), let image = UIImage(data: data) else { return nil }
        self.init(image: image, name: name)
    }

    /// Opacity of the picture laid over the object.
    static let overlayOpacity = 0.28
    /// Where the picture sits: the object's projected box, with the picture kept in proportion inside it.
    static func fitted(imageSize: CGSize, in rect: CGRect) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0, rect.width > 0, rect.height > 0 else { return rect }
        let scale = min(rect.width / imageSize.width, rect.height / imageSize.height)
        let size = CGSize(width: imageSize.width * scale, height: imageSize.height * scale)
        return CGRect(x: rect.midX - size.width / 2, y: rect.midY - size.height / 2, width: size.width, height: size.height)
    }
}

/// The small "what you're scanning" picture at the screen's edge. Tap it to hide or show the faint copy over the object.
struct GhostCorner: View {
    let ghost: ProductGhost
    @Binding var overlayOn: Bool

    var body: some View {
        Button { overlayOn.toggle() } label: {
            VStack(spacing: 4) {
                Image(uiImage: ghost.image)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 56, height: 56)
                    .background(Palette.smoke, in: RoundedRectangle(cornerRadius: Radius.card))
                    .clipShape(RoundedRectangle(cornerRadius: Radius.card))
                    .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(Palette.chalk.opacity(overlayOn ? 0.9 : 0.35),
                                                                                       lineWidth: Line.hairline))
                Text(ghost.name ?? "Your product")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Palette.chalk)
                    .lineLimit(1)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background { SmokeFill(shape: Capsule()) }
            }
            .frame(minWidth: 64, minHeight: 64)
        }
        .buttonStyle(PressableCardStyle())
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .accessibilityLabel("What you're scanning: \(ghost.name ?? "your product")")
        .accessibilityHint(overlayOn ? "Tap to hide the faint outline over the object" : "Tap to show a faint outline over the object")
    }
}

/// The picture laid faintly over the object's projected box, in proportion. Never takes touches.
struct GhostOnObject: View {
    let ghost: ProductGhost
    let rect: CGRect

    var body: some View {
        let frame = ProductGhost.fitted(imageSize: ghost.image.size, in: rect)
        Image(uiImage: ghost.image)
            .resizable()
            .frame(width: frame.width, height: frame.height)
            .position(x: frame.midX, y: frame.midY)
            .opacity(ProductGhost.overlayOpacity)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

#if DEBUG
extension ProductGhost {
    /// A drawn stand-in for previews and the screenshot job (`--demo-ghost`): a lamp-like silhouette, not a product render.
    static var demo: ProductGhost {
        let size = CGSize(width: 240, height: 320)
        let image = UIGraphicsImageRenderer(size: size).image { ctx in
            let g = ctx.cgContext
            UIColor.white.setFill()
            g.fillEllipse(in: CGRect(x: 40, y: 20, width: 160, height: 120))                    // shade
            g.fill(CGRect(x: 114, y: 140, width: 12, height: 130))                              // stem
            UIBezierPath(roundedRect: CGRect(x: 60, y: 266, width: 120, height: 30), cornerRadius: 10).fill()   // base
        }
        return ProductGhost(image: image, name: "Desk lamp")
    }
}

private struct GhostCornerPreview: View {
    @State private var on = true
    var body: some View {
        ZStack {
            Color.gray
            GhostCorner(ghost: .demo, overlayOn: $on)
        }
    }
}

#Preview("Ghost corner") { GhostCornerPreview() }
#endif

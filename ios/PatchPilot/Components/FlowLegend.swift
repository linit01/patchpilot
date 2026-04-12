import SwiftUI

/// Wrapping legend for chart OS colors + Failed indicator
struct FlowLegend: View {
    let osNames: [String]

    var body: some View {
        // Combine OS names + Failed at the end
        let items: [(String, Color)] = osNames.map { ($0, osColor($0)) }
            + [("Failed", Theme.red)]

        FlowLayout(spacing: 8) {
            ForEach(items, id: \.0) { name, color in
                HStack(spacing: 4) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(color)
                        .frame(width: 10, height: 10)
                    Text(name)
                        .font(.caption2)
                        .foregroundColor(Theme.textSecondary)
                }
            }
        }
    }

    private func osColor(_ os: String) -> Color {
        switch os {
        case "Debian", "debian":   return Theme.amber
        case "Darwin", "darwin":   return Theme.blue
        case "Windows", "windows": return Theme.greenBright
        case "RedHat", "redhat":   return Theme.purple
        case "Ubuntu", "ubuntu":   return Theme.teal
        default:                   return Theme.lcarsBlue
        }
    }
}

/// Simple left-to-right wrapping layout
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 0
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0

        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > width, x > 0 {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0

        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: .unspecified)
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

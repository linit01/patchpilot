import SwiftUI

/// Dashboard stat card matching the web UI cards
struct StatsCard: View {
    let title: String
    let value: Int
    let icon: String
    let color: Color

    var body: some View {
        VStack(spacing: 8) {
            HStack {
                Image(systemName: icon)
                    .font(.title3)
                    .foregroundColor(color)
                Spacer()
            }

            HStack {
                Text("\(value)")
                    .font(.system(size: 28, weight: .bold, design: .rounded))
                    .foregroundColor(Theme.textPrimary)
                Spacer()
            }

            HStack {
                Text(title)
                    .font(.caption)
                    .foregroundColor(Theme.textSecondary)
                Spacer()
            }
        }
        .padding()
        .background(Theme.bgCard)
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Theme.border, lineWidth: 1)
        )
    }
}

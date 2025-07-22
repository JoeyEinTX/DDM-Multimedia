#!/usr/bin/env python3
"""
Animation Generator Command Line Tool

Run this script to generate the complete racing animation library.
"""

import sys
import os

# Add the ddm directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from ddm.utils.ai_animation_engine import generate_racing_animations
    from ddm.utils.logger import get_logger
except ImportError as e:
    print(f"Import error: {e}")
    print("Make sure you're running this from the pi_controller directory")
    sys.exit(1)

logger = get_logger(__name__)


def main():
    """Main function to generate racing animations."""
    print("🏇 DDM Racing Animation Generator")
    print("=" * 50)
    print("Starting generation of racing animations...")
    print()
    
    try:
        # Generate the animations
        result = generate_racing_animations()
        
        if result:
            animations = result.get('animations', {})
            print(f"✅ Successfully generated {len(animations)} animations!")
            print()
            print("Generated animations by phase:")
            
            # Group by phase
            phases = {}
            for name, data in animations.items():
                phase = data.get('phase', 'unknown')
                if phase not in phases:
                    phases[phase] = []
                phases[phase].append(name)
            
            for phase, animation_names in phases.items():
                print(f"  📂 {phase.replace('_', ' ').title()}:")
                for anim_name in animation_names:
                    print(f"    ✨ {anim_name}")
                print()
            
            print(f"📁 Animation library saved to:")
            print("   • ddm/static/data/racing_animations_latest.json")
            print(f"   • ddm/static/data/racing_animations_{result['metadata']['generated_at'].replace(':', '-').replace('.', '-')}.json")
            print()
            print("🎉 Animation generation complete!")
            print()
            print("Next steps:")
            print("1. Start your DDM Racing System server")
            print("2. Visit http://localhost:5000/admin for the control panel")
            print("3. Use the API endpoints:")
            print("   • GET /api/animation/library - View generated animations")
            print("   • GET /api/animation/phases - See all racing phases")
            print("   • GET /api/animation/preview/{name} - Preview animation")
            
        else:
            print("❌ Animation generation failed!")
            print("Check the logs for more details.")
            
    except Exception as e:
        print(f"❌ Error during animation generation: {e}")
        logger.error(f"Animation generation error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()

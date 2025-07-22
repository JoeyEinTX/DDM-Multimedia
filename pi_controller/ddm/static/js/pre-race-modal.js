// Pre-Race Modal Button Logic
// Handles button clicks for the dedicated pre-race modal

document.addEventListener('DOMContentLoaded', function () {
    const startupBtn = document.getElementById('startup-btn');
    const anim1Btn = document.getElementById('anim1-btn');
    const anim2Btn = document.getElementById('anim2-btn');
    const anim3Btn = document.getElementById('anim3-btn');

    if (startupBtn) {
        startupBtn.addEventListener('click', function () {
            // TODO: Replace with actual startup logic
            console.log('[Pre-Race Modal] Startup button clicked');
            // Example: Close modal after click
            const modalEl = document.getElementById('preRaceModal');
            if (modalEl && window.bootstrap) {
                const modal = window.bootstrap.Modal.getInstance(modalEl) || new window.bootstrap.Modal(modalEl);
                modal.hide();
            }
        });
    }
    if (anim1Btn) {
        anim1Btn.addEventListener('click', function () {
            console.log('[Pre-Race Modal] Animation 1 button clicked');
        });
    }
    if (anim2Btn) {
        anim2Btn.addEventListener('click', function () {
            console.log('[Pre-Race Modal] Animation 2 button clicked');
        });
    }
    if (anim3Btn) {
        anim3Btn.addEventListener('click', function () {
            console.log('[Pre-Race Modal] Animation 3 button clicked');
        });
    }
});

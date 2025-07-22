// Betting Open Modal Button Logic
// Handles button clicks for the dedicated betting open modal

document.addEventListener('DOMContentLoaded', function () {
    const warn60Btn = document.getElementById('warn-60-btn');
    const warn30Btn = document.getElementById('warn-30-btn');
    const warn15Btn = document.getElementById('warn-15-btn');
    const warn5Btn = document.getElementById('warn-5-btn');

    function closeModal() {
        const modalEl = document.getElementById('bettingOpenModal');
        if (modalEl && window.bootstrap) {
            const modal = window.bootstrap.Modal.getInstance(modalEl) || new window.bootstrap.Modal(modalEl);
            modal.hide();
        }
    }
    if (warn60Btn) {
        warn60Btn.addEventListener('click', function () {
            console.log('[Betting Open Modal] 60 Minute Warning button clicked');
            closeModal();
        });
    }
    if (warn30Btn) {
        warn30Btn.addEventListener('click', function () {
            console.log('[Betting Open Modal] 30 Minute Warning button clicked');
            closeModal();
        });
    }
    if (warn15Btn) {
        warn15Btn.addEventListener('click', function () {
            console.log('[Betting Open Modal] 15 Minute Warning button clicked');
            closeModal();
        });
    }
    if (warn5Btn) {
        warn5Btn.addEventListener('click', function () {
            console.log('[Betting Open Modal] 5 Minute Warning button clicked');
            closeModal();
        });
    }
});

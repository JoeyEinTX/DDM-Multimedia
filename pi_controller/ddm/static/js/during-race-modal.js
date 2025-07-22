// During Race Modal Button Logic
// Handles button clicks for the dedicated during race modal

document.addEventListener('DOMContentLoaded', function () {
    const offBtn = document.getElementById('off-btn');
    const stretchBtn = document.getElementById('stretch-btn');
    const finishBtn = document.getElementById('finish-btn');

    if (offBtn) {
        offBtn.addEventListener('click', function () {
            console.log("[During Race Modal] AND THEY'RE OFF button clicked");
        });
    }
    if (stretchBtn) {
        stretchBtn.addEventListener('click', function () {
            console.log("[During Race Modal] AND DOWN THE STRETCH button clicked");
        });
    }
    if (finishBtn) {
        finishBtn.addEventListener('click', function () {
            console.log("[During Race Modal] FINISH button clicked");
        });
    }
});

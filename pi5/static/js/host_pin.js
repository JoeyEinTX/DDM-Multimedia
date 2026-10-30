// host_pin.js - a host page whose PIN stops working (it was changed in pi5/.env) goes back to
// the PIN form: any request of the page that pi5 answers 401 reloads it, and the page asked
// for without the cookie is the form (host_pin.py). On the host pages only: on the guest's,
// a 401 is a secret that is not theirs.
(function () {
    'use strict';
    const request = window.fetch;
    window.fetch = function () {
        return request.apply(this, arguments).then(function (response) {
            if (response.status === 401) location.reload();
            return response;
        });
    };
})();

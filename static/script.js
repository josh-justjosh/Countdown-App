document.addEventListener('DOMContentLoaded', (event) => {
    const socket = io();

    const itemCountdownForm = document.getElementById('item-countdown-form');
    const vtCountdownForm = document.getElementById('vt-countdown-form');
    const activeCountdownsDiv = document.getElementById('active-countdowns');
    const noCountdownsMessage = document.getElementById('no-countdowns-message');

    // Helper to get selected verbalization intervals
    function getVerbalizationConfig(formId) {
        const form = document.getElementById(formId);
        const activeIntervals = Array.from(form.querySelectorAll('input[name="interval"]:checked, input[name="vt-interval"]:checked'))
                                     .map(cb => {
                                         // Map values back to their string keys used in Python VERBAL_INTERVALS
                                         const val = parseInt(cb.value);
                                         if (val === 3600) return '1h';
                                         if (val === 2700) return '45m';
                                         if (val === 1800) return '30m';
                                         if (val === 1200) return '20m';
                                         if (val === 900) return '15m';
                                         if (val === 600) return '10m';
                                         if (val === 300) return '5m';
                                         if (val === 180) return '3m';
                                         if (val === 120) return '2m';
                                         return `${val}s`; // For seconds
                                     });
        const countdownFrom = form.querySelector('.countdown-from-select').value;
        const verbalize10sOn5s = form.querySelector('input[name="verbalize-10s-on-5s"], input[name="vt-verbalize-10s-on-5s"]').checked;
        const formatString = form.querySelector(`#${formId.replace('-form', '')}-format-string`).value;

        return {
            active_intervals: activeIntervals,
            countdown_from_10_5_secs: countdownFrom,
            verbalize_10s_on_5s_countdown: verbalize10sOn5s,
            format_string: formatString
        };
    }

    // Handle 5s/10s countdown checkbox visibility
    document.querySelectorAll('.countdown-from-select').forEach(select => {
        select.addEventListener('change', function() {
            const verbalize10sOption = this.closest('.verbal-options').querySelector('.verbalize-10s-option');
            if (this.value === '5s') {
                verbalize10sOption.style.display = 'inline-block';
            } else {
                verbalize10sOption.style.display = 'none';
                verbalize10sOption.querySelector('input').checked = false; // Uncheck if hidden
            }
        });
    });

    // Set current datetime for item countdown input
    const now = new Date();
    const year = now.getFullYear();
    const month = (now.getMonth() + 1).toString().padStart(2, '0');
    const day = now.getDate().toString().padStart(2, '0');
    const hours = now.getHours().toString().padStart(2, '0');
    const minutes = now.getMinutes().toString().padStart(2, '0');
    const seconds = now.getSeconds().toString().padStart(2, '0');
    document.getElementById('target-datetime').value = `${year}-${month}-${day}T${hours}:${minutes}:${seconds}`;


    itemCountdownForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const name = document.getElementById('item-name').value;
        const targetDateTime = document.getElementById('target-datetime').value; // YYYY-MM-DDTHH:MM:SS
        const verbalizationConfig = getVerbalizationConfig('item-countdown-form');

        // Convert datetime-local format to YYYY-MM-DD HH:MM:SS for Python
        const pythonTargetDateTime = targetDateTime.replace('T', ' ');

        socket.emit('start_item_countdown', {
            name: name,
            target_time: pythonTargetDateTime,
            verbalization_config: verbalizationConfig
        });
    });

    vtCountdownForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const name = document.getElementById('vt-name').value;
        const vmixIp = document.getElementById('vmix-ip').value;
        const vmixPort = parseInt(document.getElementById('vmix-port').value, 10);
        const vmixFallback1 = document.getElementById('vmix-fallback1').value;
        const vmixFallback2 = document.getElementById('vmix-fallback2').value;
        const qlabIp = document.getElementById('qlab-ip').value;
        const qlabPort = parseInt(document.getElementById('qlab-port').value, 10);
        // const qlabCueId = document.getElementById('qlab-cue-id').value; // If implemented

        const verbalizationConfig = getVerbalizationConfig('vt-countdown-form');

        const vtApiConfig = {
            vmix_ip: vmixIp || null,
            vmix_port: vmixPort || null,
            vmix_fallback1_input: vmixFallback1 || null,
            vmix_fallback2_input: vmixFallback2 || null,
            qlab_ip: qlabIp || null,
            qlab_port: qlabPort || null,
            // qlab_tracked_cue_id: qlabCueId || null // If implemented
        };

        socket.emit('start_vt_countdown', {
            name: name,
            vt_api_config: vtApiConfig,
            verbalization_config: verbalizationConfig
        });
    });

    // Function to create/update a countdown card in the UI
    const countdownCards = {}; // Store references to countdown elements

    function updateCountdownUI(data) {
        let card = countdownCards[data.id];

        if (!card) {
            // Create new card
            card = document.createElement('div');
            card.className = 'countdown-card';
            card.id = `countdown-${data.id}`;
            card.innerHTML = `
                <div>
                    <h3>${data.name} (${data.type.toUpperCase()})</h3>
                    <p>Status: <span class="status">${data.status}</span></p>
                </div>
                <div class="time-display">${data.formatted_time}</div>
                <button class="stop-btn" data-id="${data.id}">Stop</button>
            `;
            activeCountdownsDiv.appendChild(card);
            countdownCards[data.id] = card;

            card.querySelector('.stop-btn').addEventListener('click', (e) => {
                const idToStop = e.target.dataset.id;
                socket.emit('stop_countdown', { id: idToStop });
            });
        } else {
            // Update existing card
            card.querySelector('.time-display').textContent = data.formatted_time;
            card.querySelector('.status').textContent = data.status;
            if (data.status === 'finished') {
                card.classList.add('finished');
                card.querySelector('.stop-btn').style.display = 'none'; // Hide stop button
            } else {
                card.classList.remove('finished');
                card.querySelector('.stop-btn').style.display = '';
            }
        }
        checkNoCountdownsMessage();
    }

    function removeCountdownUI(id) {
        const card = countdownCards[id];
        if (card) {
            card.remove();
            delete countdownCards[id];
            console.log(`Removed countdown ${id} from UI.`);
        }
        checkNoCountdownsMessage();
    }

    function checkNoCountdownsMessage() {
        if (Object.keys(countdownCards).length === 0) {
            noCountdownsMessage.style.display = 'block';
        } else {
            noCountdownsMessage.style.display = 'none';
        }
    }


    // SocketIO event handlers
    socket.on('connect', () => {
        console.log('Connected to server');
        // Request all active countdowns on connect to populate UI
        socket.emit('get_all_countdowns');
    });

    socket.on('disconnect', () => {
        console.log('Disconnected from server');
    });

    socket.on('countdown_started_response', (response) => {
        if (response.success) {
            console.log(`Countdown '${response.name}' (ID: ${response.id}) started.`);
            // The initial 'countdown_update' will come shortly after start,
            // so no need to create the card immediately here.
        } else {
            alert(`Failed to start countdown: ${response.message}`);
        }
    });

    socket.on('countdown_update', (data) => {
        updateCountdownUI(data);
    });

    socket.on('countdown_removed', (data) => {
        removeCountdownUI(data.id);
        console.log(`Countdown ${data.id} was removed.`);
    });

    socket.on('countdown_stopped_response', (response) => {
        if (response.success) {
            console.log(`Countdown '${response.id}' stopped successfully.`);
            removeCountdownUI(response.id);
        } else {
            alert(`Failed to stop countdown: ${response.message}`);
        }
    });

    socket.on('initial_countdown_data', (data_list) => {
        // Clear existing cards if any, and then populate
        Object.keys(countdownCards).forEach(id => removeCountdownUI(id));
        if (data_list && data_list.length > 0) {
            data_list.forEach(data => updateCountdownUI(data));
        }
    });

    checkNoCountdownsMessage(); // Initial check on load
});
/**
 * DogeFeedX Shell Registration Stub
 * 
 * A compatible stub for DogeFeedX apps to register with the desktop shell's dock.
 * When the shell hook is implemented on main, this stub provides the expected interface.
 * Apps call window.DogeFeedX.registerApp() to add themselves to the dock.
 * 
 * Usage:
 *   window.DogeFeedX.registerApp({
 *     id: 'explorer',
 *     name: 'Explorer',
 *     icon: '/dogefeedx/icon-explorer.png',
 *     open: function() { ... }
 *   });
 */
(function() {
  'use strict';

  var registeredApps = [];
  var dockElement = null;
  var onReadyCallbacks = [];
  var isReady = false;

  function createDockIcon(app) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'dfx-dock-item';
    btn.title = app.name;
    btn.setAttribute('data-app-id', app.id);
    
    if (app.icon) {
      var img = document.createElement('img');
      img.src = app.icon;
      img.alt = app.name;
      img.draggable = false;
      btn.appendChild(img);
    } else {
      var span = document.createElement('span');
      span.className = 'dfx-dock-letter';
      span.textContent = (app.name || '?').charAt(0).toUpperCase();
      btn.appendChild(span);
    }

    btn.addEventListener('click', function() {
      if (typeof app.open === 'function') {
        app.open();
      } else if (app.url) {
        if (window.arcadeShell && typeof window.arcadeShell.open === 'function') {
          window.arcadeShell.open(app.url, app.name);
        } else {
          window.location.href = app.url;
        }
      }
    });

    return btn;
  }

  function renderDock() {
    if (!dockElement) return;
    
    var fragment = document.createDocumentFragment();
    registeredApps.forEach(function(app) {
      fragment.appendChild(createDockIcon(app));
    });
    
    dockElement.innerHTML = '';
    dockElement.appendChild(fragment);
  }

  function initDock() {
    dockElement = document.getElementById('dfx-dock');
    if (!dockElement) {
      dockElement = document.createElement('div');
      dockElement.id = 'dfx-dock';
      dockElement.className = 'dfx-dock';
      document.body.appendChild(dockElement);
    }
    
    renderDock();
    isReady = true;
    
    onReadyCallbacks.forEach(function(cb) {
      try { cb(); } catch (e) { console.error('DogeFeedX: onReady callback error', e); }
    });
    onReadyCallbacks = [];
  }

  window.DogeFeedX = {
    version: '1.0.0',
    
    registerApp: function(app) {
      if (!app || !app.id) {
        console.warn('DogeFeedX: registerApp requires an app object with an id');
        return false;
      }
      
      var exists = registeredApps.some(function(a) { return a.id === app.id; });
      if (exists) {
        console.warn('DogeFeedX: App with id "' + app.id + '" is already registered');
        return false;
      }
      
      registeredApps.push({
        id: app.id,
        name: app.name || app.id,
        icon: app.icon || null,
        url: app.url || null,
        open: app.open || null
      });
      
      if (dockElement) {
        renderDock();
      }
      
      return true;
    },
    
    unregisterApp: function(appId) {
      var index = -1;
      registeredApps.some(function(a, i) {
        if (a.id === appId) { index = i; return true; }
        return false;
      });
      
      if (index >= 0) {
        registeredApps.splice(index, 1);
        if (dockElement) renderDock();
        return true;
      }
      return false;
    },
    
    getApps: function() {
      return registeredApps.slice();
    },
    
    onReady: function(callback) {
      if (typeof callback !== 'function') return;
      if (isReady) {
        callback();
      } else {
        onReadyCallbacks.push(callback);
      }
    },
    
    openInWindow: function(url, name) {
      if (window.arcadeShell && typeof window.arcadeShell.open === 'function') {
        window.arcadeShell.open(url, name);
      } else {
        window.open(url, name || '_blank');
      }
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDock);
  } else {
    initDock();
  }
})();

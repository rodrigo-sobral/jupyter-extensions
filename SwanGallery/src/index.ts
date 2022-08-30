import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { requestAPI } from './handler';

import { ICommandPalette } from '@jupyterlab/apputils';

import { IFrame, MainAreaWidget } from '@jupyterlab/apputils';
import { ILauncher } from '@jupyterlab/launcher';
import { swanGalleryIcon } from './icons';

async function activate(
  app: JupyterFrontEnd,
  palette: ICommandPalette,
  launcher: ILauncher
): Promise<void> {
  console.log('JupyterLab extension SwanGallery is activated!');
  let flag = true;
  const command = 'swangallery:open';
  app.commands.addCommand(command, {
    label: 'SWAN Gallery',
    icon: swanGalleryIcon,
    execute: () => {
      const content = new IFrame({
        sandbox: [
          'allow-scripts',
          'allow-same-origin',
          'allow-modals',
          'allow-downloads'
        ]
      });

      //Avoid multiple executes of the same event
      if (flag === true) {
        flag = false;
        window.addEventListener('message', event => {
          requestAPI<any>(event.data, 'notebook')
            .then(data => {
              app.commands.execute('filebrowser:open-path', {
                path: data.path,
                showBrowser: false
              });
            })
            .catch(reason => {
              alert('Failed to download notebook');
              console.error(
                `The SwanGallery server extension appears to be missing.\n${reason}`
              );
            });
        });
      }

      content.url = 'https://yasser-gallery.docs.cern.ch/';
      content.title.label = 'SwanGallery';
      const widget = new MainAreaWidget({ content });
      widget.id = 'swan-gallery';
      widget.title.closable = true;
      app.shell.add(widget, 'main');
      app.shell.activateById(widget.id);
    }
  });

  palette.addItem({ command, category: 'SWAN' });

  if (launcher) {
    launcher.add({
      command: command,
      category: 'Other',
      rank: 1
    });
  }
}

const extension: JupyterFrontEndPlugin<void> = {
  id: 'swangallery',
  requires: [ICommandPalette, ILauncher],
  autoStart: true,
  activate: activate
};

export default extension;

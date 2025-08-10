# hass-uplift-desk
An integration for Home Assistant to control Uplift standing desks

<!--
*** This readme is inspired by the Best-README-Template available at https://github.com/othneildrew/Best-README-Template. Thanks to othneildrew for the inspiration!
-->


<!-- PROJECT SHIELDS -->
<!--
*** I'm using markdown "reference style" links for readability.
*** Reference links are enclosed in brackets [ ] instead of parentheses ( ).
*** See the bottom of this document for the declaration of the reference variables
*** for contributors-url, forks-url, etc. This is an optional, concise syntax you may use.
*** https://www.markdownguide.org/basic-syntax/#reference-style-links
-->
[![Contributors][contributors-shield]][contributors-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
<!-- [![Forks][forks-shield]][forks-url] -->



<!-- PROJECT LOGO -->
<br />
<p align="center">
  <h1 align="center">HASS Uplift Desk</h3>

  <p align="center">
    An integration for Home Assistant to control Uplift standing desks
    <br />
    <a href="https://github.com/Bennett-Wendorf/hass-uplift-desk"><strong>Explore the docs »</strong></a>
    <br />
    <br />
    <a href="https://github.com/Bennett-Wendorf/hass-uplift-desk/issues">Report Bug</a>
    ·
    <a href="https://github.com/Bennett-Wendorf/hass-uplift-desk/issues">Request Feature</a>
  </p>
</p>



<!-- TABLE OF CONTENTS -->
<details open="open">
  <summary>Table of Contents</summary>
  <ol>
    <li><a href="#about-the-project">About The Project</a></li>
    <li><a href="#getting-started">Getting Started</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgements">Acknowledgements</a></li>
  </ol>
</details>



<!-- ABOUT THE PROJECT -->
## About The Project

This is an **UNOFFICIAL** Home Assistant integration for control of Uplift Desk standing desks over Bluetooth Low Energy (BLE). For this library to work, you must have the [Uplift Bluetooth Adapter](https://www.upliftdesk.com/bluetooth-adapter-for-uplift-desk/?15775=12278) installed in a compatible desk. See their website for a better understanding of desk compatibility. 

This integration relies on the uplift-ble Python package, which can be found on [PyPi](https://pypi.org/project/uplift-ble/) and [GitHub](https://github.com/librick/uplift-ble). Please head over there if you have issues related to that library or would like to contribute to functionality :)

> Note: When using this project, no other device can be connected to the desk or it will be undiscoverable. This means that the Uplift Desk app needs to be either disconnected or closed for this application to work.

The integration currently provides 3 entities:
1. A sensor for the current height of the desk. This will update automatically as your desk is moving, though it is not instantaneous and should not be relied on for safety.
2. A button to move the desk to its configured preset 1.
3. A button to move the desk to its configured preset 2.


<!-- Getting Started -->
## Getting Started

### Install via HACS
This is the easiest way to install HASS Uplift Desk. Click the button below to get started!

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Bennett-Wendorf&repository=hass-uplift-desk&category=integration)


<!-- CONTRIBUTING -->
## Contributing

Contributions are what make the open source community such an amazing place to be learn, inspire, and create. Any contributions you make are **greatly appreciated**.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

If you find an issue in existing code, feel free to use the above procedure to generate a change, or open an [issue](https://github.com/Bennett-Wendorf/hass-uplift-desk/issues) for me to fix it.


<!-- LICENSE -->
## License

Distributed under the MIT License. See `LICENSE` for more information.



<!-- CONTACT -->
## Contact

Bennett Wendorf - [Website](https://bennettwendorf.dev/) - bennett@bennettwendorf.dev



<!-- ACKNOWLEDGEMENTS -->
## Acknowledgements
* [Img Shields](https://shields.io)
* [uplift-ble](https://github.com/librick/uplift-ble)


<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
[contributors-shield]: https://img.shields.io/github/contributors/bennett-Wendorf/hass-uplift-desk.svg?style=flat&color=informational
[contributors-url]: https://github.com/Bennett-Wendorf/hass-uplift-desk/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/bennett-Wendorf/hass-uplift-desk.svg?style=flat
[forks-url]: https://github.com/Bennett-Wendorf/hass-uplift-desk/network/members
[stars-shield]: https://img.shields.io/github/stars/bennett-Wendorf/hass-uplift-desk.svg?style=flat&color=yellow
[stars-url]: https://github.com/Bennett-Wendorf/hass-uplift-desk/stargazers
[issues-shield]: https://img.shields.io/github/issues/bennett-Wendorf/hass-uplift-desk.svg?style=flat&color=red
[issues-url]: https://github.com/Bennett-Wendorf/hass-uplift-desk/issues
[license-shield]: https://img.shields.io/github/license/bennett-Wendorf/hass-uplift-desk.svg?style=flat
[license-url]: https://github.com/Bennett-Wendorf/hass-uplift-desk/blob/main/LICENSE
